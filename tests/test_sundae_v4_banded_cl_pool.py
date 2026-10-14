"""SundaeV4BandedCLPool against the preview chain.

Most tests use a vault carrying preview pool P1's ladder and reserves: the vault
is synthetic (the factory builds the datum), but the config hashes to the
commitment P1 holds. ``sundae_v4_banded_cl_preview_scoops.json`` holds recorded
preview scoops of the deployed module on four ladders, from single steps to a
16-band walk and through constant-sum bins both ways, which the pool type must
reproduce step for step.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.multi_asset import AbstractMultiAssetPoolState
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLConfig
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4BandedCLPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import banded_cl_ladder
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import marginal_price
from charli3_dendrite.dexs.core.errors import InvalidPoolError

from tests.sundae_v4_vault_factory import build_banded_cl_vault_utxo
from tests.test_sundae_v4_banded_cl_math import P1_CLOSING
from tests.test_sundae_v4_banded_cl_math import P1_QUOTES
from tests.test_sundae_v4_banded_cl_math import P1_RESERVES
from tests.test_sundae_v4_banded_cl_math import P1_STARTS
from tests.test_sundae_v4_banded_cl_math import P1_WITNESS

TOKEN_POLICY = "09169bb6f5ff5b246d65d65935b2222cc53b5e677d7ed22771878972"
TOKENA = TOKEN_POLICY + "74" + "4f4b454e41"  # tOKENA
TOKENC = TOKEN_POLICY + "74" + "4f4b454e43"  # tOKENC
P1_COMMITMENT = "361006908c76928585007cd24aeec231db3d571df4d04a76be5c6b8f0f4313c9"
P1_TOTAL_LP = 1_000_049_689
SCOOPS = json.loads(
    (Path(__file__).parent / "sundae_v4_banded_cl_preview_scoops.json").read_text()
)


@pytest.fixture(autouse=True)
def _preview() -> Iterator[None]:
    SundaeV4Vault.select_network("preview")
    SundaeV4Vault.clear_config_cache()
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_config_cache()


def _p1(
    reserves: tuple[int, int] = P1_RESERVES,
    **kwargs: object,
) -> tuple[SundaeV4Vault, BandedCLConfig]:
    values, config = build_banded_cl_vault_utxo(
        [(TOKENA, reserves[0]), (TOKENC, reserves[1])],
        starts=P1_STARTS,
        closing=P1_CLOSING,
        total_lp=P1_TOTAL_LP,
        **kwargs,  # type: ignore[arg-type]
    )
    return SundaeV4Vault.model_validate(values), config


def _pool(
    reserves: tuple[int, int] = P1_RESERVES, **kwargs: object
) -> SundaeV4BandedCLPool:
    vault, config = _p1(reserves, **kwargs)
    module = SundaeV4Deployment.for_network("preview").banded_cl_hash
    vault.supply_module_config(module, config)
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4BandedCLPool)
    return pool


def test_the_ladder_commits_to_the_hash_the_preview_pools_hold() -> None:
    vault, config = _p1()
    module = SundaeV4Deployment.for_network("preview").banded_cl_hash
    assert module_config_hash(config).hex() == P1_COMMITMENT
    assert vault.module_state[module].hex() == P1_COMMITMENT
    assert vault.invariant_modules() == [(100, module, "banded_cl")]


def test_the_manifest_lists_the_banded_module_under_its_upstream_title() -> None:
    preview = SundaeV4Deployment.for_network("preview")
    preprod = SundaeV4Deployment.for_network("preprod")
    title = "banded_concentrated_liquidity.withdraw"
    assert preview.banded_cl_hash.hex().startswith("33485bba")
    assert preprod.banded_cl_hash.hex().startswith("f75a1c14")
    superseded = preview.validator(title + ".superseded1")
    for deployment, module in (
        (preview, preview.banded_cl_hash),
        (preview, superseded),
        (preprod, preprod.banded_cl_hash),
    ):
        assert deployment.module_kind(module) == "banded_cl"
    assert preview.reference(title)[0].startswith("797d5fe6")
    assert preview.reference(title + ".superseded1")[0].startswith("1eba92d9")
    assert preview.settings["bcl-pool"]["tx_hash"].startswith("dbd325c7")
    assert preprod.settings["bcl-pool"]["tx_hash"].startswith("e28742a8")
    mainnet = SundaeV4Deployment.for_network("mainnet")
    assert mainnet.module_kind(preview.banded_cl_hash) is None
    with pytest.raises(KeyError):
        _ = mainnet.banded_cl_hash


def test_the_ladder_commits_to_the_hash_a_preprod_pool_holds() -> None:
    SundaeV4Vault.select_network("preprod")
    vaults = json.loads(
        (Path(__file__).parent / "sundae_v4_banded_cl_vaults.json").read_text()
    )
    vault = SundaeV4Vault.model_validate(
        {**vaults["preprod_pool"], "block_time": 0, "block_index": 0}
    )
    module = SundaeV4Deployment.for_network("preprod").banded_cl_hash
    assert vault.invariant_modules() == [(100, module, "banded_cl")]
    config = BandedCLConfig.from_cbor(vaults["preprod_config"])
    assert config.to_cbor_hex() == vaults["preprod_config"]
    assert module_config_hash(config) == vault.module_state[module]
    vault.supply_module_config(module, config)
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4BandedCLPool)
    witness = pool.witness()
    assert witness is not None
    assert (witness.band, witness.counter) == (2, 401_192_959)
    out = pool.get_amount_out(Assets(**{pool.asset_a: 1_000}), pool.asset_b)[0]
    assert 0 < out.quantity() < 1_000


def test_the_vault_is_one_banded_cl_pool() -> None:
    pool = _pool()
    assert isinstance(pool, AbstractMultiAssetPoolState)
    assert pool.tag == 100
    assert pool.pool_id == f"{pool.vault.pool_id}:100"
    assert pool.units() == [TOKENA, TOKENC]
    assert (pool.asset_a, pool.asset_b) == (TOKENA, TOKENC)
    assert pool.dex() == "SundaeSwapV4"
    assert pool.stake_address == SundaeV4Deployment.for_network("preview").order_address
    assert pool.deposit().quantity() == 2_000_000
    assert pool.swap_forward is True
    assert pool.ladder.n == 8
    assert pool.ladder.weight_total == 8


def test_the_witness_is_derived_from_the_reserves() -> None:
    pool = _pool()
    witness = pool.witness()
    assert witness is not None
    assert (witness.counter, witness.band) == P1_WITNESS
    assert pool.active_band == 3
    assert pool.witness() is witness  # memoised on the reserves


@pytest.mark.parametrize(("dx", "a_in", "want", "bands"), P1_QUOTES)
def test_get_amount_out_is_the_recorded_output(
    dx: int, a_in: bool, want: int, bands: tuple[int, ...]
) -> None:
    pool = _pool()
    unit_in, unit_out = (TOKENA, TOKENC) if a_in else (TOKENC, TOKENA)
    out, impact = pool.get_amount_out(Assets(**{unit_in: dx}), unit_out)
    assert out.unit() == unit_out
    assert out.quantity() == want
    assert 0.0 < impact < 0.05
    assert pool.quote(unit_in, dx).bands == bands
    # The quote did not move the pool.
    assert pool.reserves.root == {TOKENA: P1_RESERVES[0], TOKENC: P1_RESERVES[1]}


@pytest.mark.parametrize(("dx", "a_in", "want", "_bands"), P1_QUOTES)
def test_get_amount_in_is_the_least_offer_reaching_the_output(
    dx: int, a_in: bool, want: int, _bands: tuple[int, ...]
) -> None:
    pool = _pool()
    unit_in, unit_out = (TOKENA, TOKENC) if a_in else (TOKENC, TOKENA)
    needed, impact = pool.get_amount_in(Assets(**{unit_out: want}), unit_in)
    assert needed.unit() == unit_in
    assert needed.quantity() <= dx
    assert pool.get_amount_out(needed, unit_out)[0].quantity() >= want
    one_less = Assets(**{unit_in: needed.quantity() - 1})
    assert pool.get_amount_out(one_less, unit_out)[0].quantity() < want
    assert 0.0 < impact < 0.05


def test_an_offer_the_ladder_cannot_absorb_whole_quotes_zero() -> None:
    pool = _pool()
    assert pool.max_output(TOKENC) == P1_RESERVES[1] + 1
    assert pool.max_output(TOKENA) == P1_RESERVES[0] + 1
    with pytest.raises(InvalidPoolError, match="no amount"):
        pool.get_amount_in(Assets(**{TOKENC: P1_RESERVES[1] + 1}), TOKENA)
    quote = pool.quote(TOKENA, 2**64 - 1)
    assert quote.amount_out == P1_RESERVES[1]
    assert quote.spent < 2**64 - 1
    out, impact = pool.get_amount_out(Assets(**{TOKENA: 2**64 - 1}), TOKENC)
    assert (out.quantity(), impact) == (0, 0.0)
    over = Assets(**{TOKENA: quote.spent + 1})
    assert pool.get_amount_out(over, TOKENC)[0].quantity() == 0
    # The whole holding is reachable by an offer the ladder absorbs in full.
    whole = pool.get_amount_in(Assets(**{TOKENC: P1_RESERVES[1]}), TOKENA)[0]
    assert whole.quantity() <= quote.spent
    assert pool.get_amount_out(whole, TOKENC)[0].quantity() == P1_RESERVES[1]


@pytest.mark.parametrize(
    ("shape", "reserves"),
    [
        # A constant-sum bin and a CL arc whose last band's full drain leaves the
        # end bounded only at some inputs.
        (
            {
                "starts": [(5490, 80000)],
                "closing": (10476, 80000),
                "weights": [2],
                "curves": [1],
                "fee_buy": [(5, 10000)],
                "fee_sell": [(11, 1009)],
            },
            (1, 26_970_464_100),
        ),
        (
            {
                "starts": [(1, 7)],
                "closing": (2, 7),
                "weights": [2],
                "curves": [0],
                "fee_buy": [(1, 2**40)],
                "fee_sell": [(0, 1)],
            },
            (206_206_244_504, 9_908_183_807),
        ),
    ],
)
def test_get_amount_in_names_an_offer_the_ladder_absorbs_at_a_full_drain(
    shape: dict, reserves: tuple[int, int]
) -> None:
    values, config = build_banded_cl_vault_utxo(
        [(TOKENA, reserves[0]), (TOKENC, reserves[1])],
        total_lp=P1_TOTAL_LP,
        **shape,
    )
    vault = SundaeV4Vault.model_validate(values)
    vault.supply_module_config(
        SundaeV4Deployment.for_network("preview").banded_cl_hash, config
    )
    (pool,) = vault.pools()
    top = pool.max_output(TOKENC) - 1
    needed = pool.get_amount_in(Assets(**{TOKENC: top}), TOKENA)[0]
    assert pool.quote(TOKENA, needed.quantity()).spent == needed.quantity()
    assert pool.get_amount_out(needed, TOKENC)[0].quantity() >= top


# P1 after a trade that buys exactly band 3's remaining 324 tOKENA: on the 3|4 edge.
P1_EDGE = (5_952_679, 6_250_312)


def test_a_pool_resting_on_a_band_edge_trades_both_ways() -> None:
    pool = _pool()
    need = pool.get_amount_in(Assets(**{TOKENA: 324}), TOKENC)[0].quantity()
    assert need == 324
    assert pool.quote(TOKENC, need).reserves_after == P1_EDGE
    edge = _pool(P1_EDGE)
    # Buying A goes through band 4; selling A through band 3.
    assert edge.get_amount_out(Assets(**{TOKENC: 10_000}), TOKENA)[0].quantity() == (
        9_969
    )
    assert edge.quote(TOKENC, 10_000).bands == (4,)
    assert edge.quote(TOKENA, 10_000).bands == (3,)
    assert edge.max_output(TOKENA) == P1_EDGE[0] + 1
    needed = edge.get_amount_in(Assets(**{TOKENA: 100}), TOKENC)[0]
    assert edge.get_amount_out(needed, TOKENA)[0].quantity() >= 100
    ladder = edge.ladder
    band_3, band_4 = edge.witness(True), edge.witness(False)
    assert (band_3.band, band_4.band) == (3, 4)
    assert edge.price(TOKENA, TOKENC) == marginal_price(band_3, ladder.bands[3], True)
    assert edge.price(TOKENC, TOKENA) == marginal_price(band_4, ladder.bands[4], False)


def test_price_on_an_edge_is_the_margin_of_the_band_each_direction_enters() -> None:
    # Band 0 is a CL arc and band 1 a constant-sum bin at the fixed price
    # 1.1 * 1.2; the reserves sit exactly on the edge at 1.1, in both bands.
    values, config = build_banded_cl_vault_utxo(
        [(TOKENA, 37_878_788), (TOKENC, 50_000_000)],
        starts=[(10, 10), (11, 10)],
        closing=(12, 10),
        curves=[0, 1],
        total_lp=10**9,
    )
    vault = SundaeV4Vault.model_validate(values)
    vault.supply_module_config(
        SundaeV4Deployment.for_network("preview").banded_cl_hash, config
    )
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4BandedCLPool)
    # Selling A prices on the arc's upper edge, 1.21 B per A; buying A in the bin.
    assert pool.price(TOKENA, TOKENC) == (60_500_000_000, 50_000_000_000)
    assert pool.price(TOKENC, TOKENA) == (100, 132)
    assert pool.quote(TOKENC, 1_320).bands == (1,)
    assert pool.get_amount_out(Assets(**{TOKENC: 1_320}), TOKENA)[0].quantity() == 997


def test_apply_swap_moves_the_reserves_and_the_witness_follows() -> None:
    pool = _pool()
    quote = pool.quote(TOKENC, 1_000)
    assert quote.bands == (3, 4)
    pool.apply_swap(Assets(**{TOKENC: 1_000}), Assets(**{TOKENA: quote.amount_out}))
    assert (
        pool.reserves.root[TOKENA],
        pool.reserves.root[TOKENC],
    ) == quote.reserves_after
    assert pool.active_band == 4
    assert pool.vault.reserves.root[TOKENA] == P1_RESERVES[0]
    # A fresh pool at the moved reserves agrees with the moved one.
    fresh = _pool(quote.reserves_after)
    assert fresh.witness() == pool.witness()
    assert fresh.get_amount_out(
        Assets(**{TOKENA: 50_000}), TOKENC
    ) == pool.get_amount_out(Assets(**{TOKENA: 50_000}), TOKENC)


def test_price_is_the_active_band_margin_in_both_directions() -> None:
    pool = _pool()
    p_in, p_out = pool.price(TOKENA, TOKENC)
    assert pool.price(TOKENC, TOKENA) == (p_out, p_in)
    # P1's ladder brackets price 1 (0.9025 to 1.1025) and the state sits in band 3.
    assert 0.95 < p_in / p_out < 1.0
    out = pool.get_amount_out(Assets(**{TOKENA: 100}), TOKENC)[0].quantity()
    assert out <= 100 * p_in // p_out
    with pytest.raises(ValueError, match="two reserves"):
        pool.price(TOKENA, TOKENA)
    with pytest.raises(ValueError, match="not a reserve"):
        pool.price("lovelace", TOKENA)


def test_a_zero_offer_quotes_zero() -> None:
    pool = _pool()
    out, impact = pool.get_amount_out(Assets(**{TOKENA: 0}), TOKENC)
    assert out.quantity() == 0
    assert impact == 0.0
    assert pool.quote(TOKENA, 0).bands == ()


@pytest.mark.parametrize(
    ("asset", "out_unit", "message"),
    [
        ({"lovelace": 1}, TOKENC, "not a reserve distinct"),
        ({TOKENC: 1}, TOKENC, "not a reserve distinct"),
        ({TOKENA: 1, TOKENC: 1}, TOKENC, "exactly one offered asset"),
        ({TOKENA: -1}, TOKENC, "negative"),
        ({TOKENA: 1}, "lovelace", "not a reserve of this pool"),
    ],
)
def test_bad_offers_are_rejected(asset: dict, out_unit: str, message: str) -> None:
    pool = _pool()
    with pytest.raises(ValueError, match=message):
        pool.get_amount_out(Assets(**asset), out_unit)


def test_liquidity_moves_scale_the_lp_supply() -> None:
    # The proportional check itself is pinned against the oracle port.
    pool = _pool()
    deposit = pool.pinned_deposit(Assets(**{TOKENA: 100_000, TOKENC: 104_657}))
    assert deposit.lp_after == P1_TOTAL_LP + deposit.lp_minted
    assert deposit.target_delta_v == deposit.lp_minted
    withdraw = pool.pinned_withdraw(50_000)
    assert withdraw.lp_after == P1_TOTAL_LP - 50_000
    assert withdraw.lp_burned == 50_000
    with pytest.raises(ValueError, match="every pool asset"):
        pool.pinned_deposit(Assets(**{TOKENA: 100_000}))


def test_a_three_reserve_vault_cannot_bind_the_module() -> None:
    values, config = build_banded_cl_vault_utxo(
        [(TOKENA, 10**6), (TOKENC, 10**6), ("lovelace", 10**6)],
        starts=P1_STARTS,
        closing=P1_CLOSING,
        total_lp=10**6,
    )
    vault = SundaeV4Vault.model_validate(values)
    vault.supply_module_config(
        SundaeV4Deployment.for_network("preview").banded_cl_hash, config
    )
    with pytest.raises(InvalidPoolError, match="prices two"):
        vault.pools()


def test_a_config_whose_index_is_not_its_bands_index_is_invalid() -> None:
    vault, config = _p1()
    index = [list(e) for e in config.index]
    index[0][0] += 1
    from pycardano import IndefiniteList

    bad = BandedCLConfig(
        bands=config.bands,
        index=IndefiniteList([IndefiniteList(e) for e in index]),
        closing=config.closing,
        weight_total=config.weight_total,
    )
    with pytest.raises(InvalidPoolError, match="index"):
        SundaeV4BandedCLPool.from_vault(vault, 100, bad)


def test_an_unpriceable_state_quotes_zero() -> None:
    # An empty pool: P1 needs a counter of at least the weight total, and no band
    # at any such counter holds nothing of both assets, so no band admits a witness.
    pool = _pool((0, 0))
    assert pool.witness() is None
    assert pool.active_band is None
    assert pool.price(TOKENA, TOKENC) == (0, 1)
    out, impact = pool.get_amount_out(Assets(**{TOKENA: 1_000}), TOKENC)
    assert out.quantity() == 0
    assert impact == 0.0


def test_public_export() -> None:
    import charli3_dendrite

    assert charli3_dendrite.SundaeV4BandedCLPool is SundaeV4BandedCLPool


def _recorded(index: int) -> BandedCLConfig:
    return BandedCLConfig.from_cbor(SCOOPS["ladders"][index])


def test_the_recorded_ladders_rebuild_their_on_chain_index() -> None:
    for index, cbor_hex in enumerate(SCOOPS["ladders"]):
        config = _recorded(index)
        assert config.to_cbor_hex() == cbor_hex
        ladder = banded_cl_ladder(
            list(config.bands), config.closing, config.weight_total
        )
        assert [list(entry) for entry in ladder.index] == [
            [int(x) for x in entry] for entry in config.index
        ]


def test_the_recorded_scoops_cover_long_walks_bins_and_both_directions() -> None:
    scoops = SCOOPS["scoops"]
    walks = {len(s["steps"]) for s in scoops}
    assert {1, 2, 15, 16} <= walks
    assert {s["a_is_input"] for s in scoops} == {True, False}
    in_bins = [
        s
        for s in scoops
        if any(
            int(_recorded(s["ladder"]).bands[step[2]].curve) == 1 for step in s["steps"]
        )
    ]
    assert {s["a_is_input"] for s in in_bins} == {True, False}


@pytest.mark.parametrize(
    "scoop",
    SCOOPS["scoops"],
    ids=[f"{s['tx'][:16]}-{i}" for i, s in enumerate(SCOOPS["scoops"])],
)
def test_the_pool_type_reproduces_a_recorded_scoop(scoop: dict) -> None:
    config = _recorded(scoop["ladder"])
    bands = list(config.bands)
    a, b = scoop["reserves"]
    values, built = build_banded_cl_vault_utxo(
        [(TOKENA, a), (TOKENC, b)],
        starts=[(band.start.num, band.start.den) for band in bands],
        closing=(config.closing.num, config.closing.den),
        weights=[band.weight for band in bands],
        curves=[band.curve for band in bands],
        fee_buy=[(band.fee_buy.num, band.fee_buy.den) for band in bands],
        fee_sell=[(band.fee_sell.num, band.fee_sell.den) for band in bands],
        total_lp=P1_TOTAL_LP,
    )
    assert built.to_cbor_hex() == config.to_cbor_hex()
    vault = SundaeV4Vault.model_validate(values)
    vault.supply_module_config(
        SundaeV4Deployment.for_network("preview").banded_cl_hash, config
    )
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4BandedCLPool)
    unit_in, unit_out = (TOKENA, TOKENC) if scoop["a_is_input"] else (TOKENC, TOKENA)
    quote = pool.quote(unit_in, scoop["offer"])
    assert quote.spent == scoop["offer"]
    assert [
        [step.amount_in, step.amount_out, step.witness.band, step.witness.counter]
        for step in quote.steps
    ] == scoop["steps"]
    out = pool.get_amount_out(Assets(**{unit_in: scoop["offer"]}), unit_out)[0]
    assert out.quantity() == sum(step[1] for step in scoop["steps"])
