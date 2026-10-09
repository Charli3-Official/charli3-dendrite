"""SundaeV4BandedCLPool on a vault carrying preview pool P1's ladder and reserves.

The vault is synthetic (the factory builds the datum), but the ladder, the reserves
and every expected number are the preview chain's: the config commits to the hash
all four preview banded pools hold, and the quotes are the recorded ones.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.multi_asset import AbstractMultiAssetPoolState
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLConfig
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4BandedCLPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
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
    if kwargs.get("module_title"):
        module = SundaeV4Deployment.for_network("preview").validator(
            str(kwargs["module_title"])
        )
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
    assert len(pool.bands) == 8
    assert pool.band_fee(3, TOKENA) == (3, 1000)
    assert pool.band_fee(3, TOKENC) == (3, 1000)
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


def test_max_output_is_the_whole_holding_plus_one() -> None:
    pool = _pool()
    assert pool.max_output(TOKENC) == P1_RESERVES[1] + 1
    assert pool.max_output(TOKENA) == P1_RESERVES[0] + 1
    with pytest.raises(InvalidPoolError, match="no amount"):
        pool.get_amount_in(Assets(**{TOKENC: P1_RESERVES[1] + 1}), TOKENA)
    # The whole holding is reachable, and the ledger-max offer overshoots it.
    whole = pool.get_amount_in(Assets(**{TOKENC: P1_RESERVES[1]}), TOKENA)[0]
    assert pool.get_amount_out(whole, TOKENC)[0].quantity() == P1_RESERVES[1]
    quote = pool.quote(TOKENA, 2**64 - 1)
    assert quote.amount_out == P1_RESERVES[1]
    assert quote.spent < 2**64 - 1


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


def test_liquidity_moves_pass_the_proportional_check_and_are_extremal() -> None:
    pool = _pool()
    before = [P1_RESERVES[0], P1_RESERVES[1]]
    lp_before = P1_TOTAL_LP
    deposit = pool.pinned_deposit(Assets(**{TOKENA: 100_000, TOKENC: 104_657}))
    after = [b + d for b, d in zip(before, deposit.deltas)]
    assert deposit.lp_after == lp_before + deposit.lp_minted
    assert deposit.target_delta_v == deposit.lp_minted
    assert all(a * lp_before >= b * deposit.lp_after for a, b in zip(after, before))
    # Minimal deltas: one base unit less on any asset fails the check.
    for i in range(2):
        shaved = list(after)
        shaved[i] -= 1
        assert not all(
            a * lp_before >= b * deposit.lp_after for a, b in zip(shaved, before)
        )
    # Maximal mint: one more LP needs more than was offered.
    offered = [100_000, 104_657]
    more = deposit.lp_after + 1
    assert any(-(-(b * more) // lp_before) - b > o for b, o in zip(before, offered))
    withdraw = pool.pinned_withdraw(50_000)
    paid = [b - p for b, p in zip(before, withdraw.payouts)]
    assert withdraw.lp_after == lp_before - 50_000
    assert withdraw.lp_burned == 50_000
    assert all(a * lp_before >= b * withdraw.lp_after for a, b in zip(paid, before))
    for i in range(2):
        greedy = list(paid)
        greedy[i] -= 1
        assert not all(
            a * lp_before >= b * withdraw.lp_after for a, b in zip(greedy, before)
        )
    with pytest.raises(ValueError, match="every pool asset"):
        pool.pinned_deposit(Assets(**{TOKENA: 100_000}))


def test_a_pool_still_bound_to_the_superseded_build_is_a_banded_pool() -> None:
    pool = _pool(module_title="banded_cl.withdraw.superseded1")
    assert pool.active_band == 3
    assert pool.get_amount_out(Assets(**{TOKENA: 1_000}), TOKENC)[0].quantity() == 996


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
