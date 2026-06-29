"""Offline structural test for the deposit/withdraw (TopupWithdraw) builder.

`build_topup_withdraw` contributes a deposit (top-up) or withdrawal to a
caller-supplied `pycardano.TransactionBuilder`, mirroring the create-loan seam: a
pool script input, the protocol-config + market reference inputs, the dToken
mint/burn, and the updated-pool + actor outputs. The caller owns the chain
context, balancing, and evaluation, so this test only asserts that the builder
ends up WIRED (the synthesized datum/mint/indices are proven on Ogmios in the
live task). It runs fully offline against a network-free chain context and a
`TopupWithdrawSnapshot` reconstructed from the captured fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.danogo.constants import PROTOCOL_CONFIG_NFT
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.transactions.build import build_topup_withdraw
from charli3_dendrite.lending.danogo.transactions.context import POOL_SCRIPT_SKH
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.danogo.transactions.context import _as_utxo
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA

# A real lovelace-supply (ADA) pool captured for deposit/withdraw -- the only Danogo
# market shape where the supply token IS the coin, so the pool output's lovelace
# carries the deposited/withdrawn supply delta (every other market keeps the supply in
# a native asset, leaving the coin as the carried min-ADA balance).
LOVELACE_POOL = "topup_zero_held_alt_tx.json"


@pytest.fixture
def tw_snap(
    script_hash, parses
) -> Callable[[str], TopupWithdrawSnapshot]:  # noqa: ANN001
    """Factory rebuilding a `TopupWithdrawSnapshot` from a captured deposit/withdraw tx.

    The fixture is a post-state tx, so the snapshot's read-side UTxOs are classified
    back out of it: the protocol-config (config NFT) yields the derived script hashes,
    the dToken mint (under `pool_skh`) names the market, the pool UTxO + market-param
    UTxO are paired by the pool NFT (`config_pool_skh` + `market_name`), and the pool
    reference script is the one whose script hash is `POOL_SCRIPT_SKH`.
    """

    def _make(name: str) -> TopupWithdrawSnapshot:
        fix = json.loads(
            (Path(__file__).parent / "fixtures" / name).read_text(),
        )
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        inputs = [_as_utxo(u) for u in fix["inputs"]]

        cfg_nft = PROTOCOL_CONFIG_NFT or ""
        cfg_policy, cfg_name = cfg_nft[:56], cfg_nft[56:]
        protocol_config = next(u for u in ref_inputs if u.holds(cfg_policy, cfg_name))
        pd = ProtocolDatum.from_cbor(protocol_config.datum)
        pool_skh = pd.pool_skh.hex()
        config_pool_skh = pd.config_pool_skh.hex()

        # The single dToken mint/burn is under the pool script hash; name = market.
        market_name = next(m[1] for m in fix["mints"] if m[0] == pool_skh)

        pool = next(
            u
            for u in inputs
            if u.holds(config_pool_skh, market_name) and parses(u.datum, PoolDatum)
        )
        market = next(u for u in ref_inputs if u.holds(config_pool_skh, market_name))
        market_info = DanogoMarket.from_market_datum(market.datum)

        pool_script_ref = next(
            u for u in ref_inputs if u.ref_script and script_hash(u) == POOL_SCRIPT_SKH
        )

        return TopupWithdrawSnapshot(
            market_name=market_name,
            pool_skh=pool_skh,
            config_pool_skh=config_pool_skh,
            oracle_skh=pd.oracle_skh.hex(),
            protocol_config=protocol_config,
            market=market,
            market_info=market_info,
            pool=pool,
            pool_script_ref=pool_script_ref,
        )

    return _make


def _mint_triples(mint) -> list[tuple[str, str, int]]:  # noqa: ANN001
    return [
        (bytes(policy).hex(), bytes(name).hex(), qty)
        for policy, names in mint.data.items()
        for name, qty in names.items()
    ]


def test_build_deposit_wires_spend_mint_outputs(
    offline_ctx, actor_addr, tw_snap
):  # noqa: ANN001
    tx_builder = TransactionBuilder(offline_ctx)
    build_topup_withdraw(
        tx_builder,
        snapshot=tw_snap("topup_tx.json"),
        actor_address=actor_addr,
        supply_change=25_000_000,  # positive = deposit (above market min_tx_amount)
    )
    assert len(tx_builder.inputs) >= 1  # pool spend
    assert len(tx_builder.outputs) >= 2  # updated pool + actor dToken output
    assert tx_builder.mint is not None  # +dToken
    assert len(tx_builder.reference_inputs) >= 2  # protocol cfg + market


def test_build_deposit_mint_is_positive(
    offline_ctx, actor_addr, tw_snap
):  # noqa: ANN001
    tx_builder = TransactionBuilder(offline_ctx)
    snap = tw_snap("topup_tx.json")
    build_topup_withdraw(
        tx_builder,
        snapshot=snap,
        actor_address=actor_addr,
        supply_change=25_000_000,
    )
    dtoken = snap.pool_skh + snap.market_name
    minted = {p + n: q for p, n, q in _mint_triples(tx_builder.mint)}
    assert minted.get(dtoken, 0) > 0  # deposit mints dTokens


def test_build_withdraw_burns_dtoken(offline_ctx, actor_addr, tw_snap):  # noqa: ANN001
    tx_builder = TransactionBuilder(offline_ctx)
    build_topup_withdraw(
        tx_builder,
        snapshot=tw_snap("withdraw_tx.json"),
        actor_address=actor_addr,
        supply_change=-25_000_000,  # negative = withdraw (above market min_tx_amount)
    )
    assert any(q < 0 for _, _, q in _mint_triples(tx_builder.mint))  # burn is negative


def test_build_rejects_below_min_tx_amount(
    offline_ctx, actor_addr, tw_snap
):  # noqa: ANN001
    snap = tw_snap("topup_tx.json")
    # The IAG market enforces a non-zero minimum; a supply change below it is the
    # one the on-chain validator rejects, so the builder fails loud first.
    assert snap.market_info.min_tx_amount > 0
    tx_builder = TransactionBuilder(offline_ctx)
    with pytest.raises(ValueError, match="minimum transaction amount"):
        build_topup_withdraw(
            tx_builder,
            snapshot=snap,
            actor_address=actor_addr,
            supply_change=snap.market_info.min_tx_amount - 1,
        )


def test_validity_window_within_topup_bound(
    offline_ctx, actor_addr, tw_snap
):  # noqa: ANN001
    tx_builder = TransactionBuilder(offline_ctx)
    build_topup_withdraw(
        tx_builder,
        snapshot=tw_snap("topup_tx.json"),
        actor_address=actor_addr,
        supply_change=25_000_000,
    )
    assert tx_builder.validity_start is not None
    assert tx_builder.ttl is not None
    assert (tx_builder.ttl - tx_builder.validity_start) * 1000 <= 360_000


# --- lovelace-supply pool output carries the deposited/withdrawn supply delta ------
#
# The topup fixtures above are native-token-supply markets, where the supply token is
# NOT the coin: the pool output's lovelace is just the carried min-ADA balance, so
# flooring the computed coin and flooring the pool's pre-action lovelace are identical.
# The ONE market shape that distinguishes them is a lovelace-supply (ADA) market: the
# supply token IS the coin, so a deposit adds (and a withdraw removes) lovelace from the
# pool's coin. `_pool_output_value` already applies that delta, so the builder must
# floor THAT computed coin -- never reset it to the pool's pre-action lovelace, which
# would drop the deposited/withdrawn ADA. This drives the real `build_topup_withdraw`
# against a captured lovelace-supply pool, mirroring the per-builder lovelace tests in
# `test_build_repay.py` / `test_build_increase_loan.py`.


def _pool_output(
    tx_builder: TransactionBuilder, snap: TopupWithdrawSnapshot
):  # noqa: ANN202
    """The builder's updated-pool output: the one output carrying the pool NFT."""
    pool_nft = (snap.config_pool_skh, snap.market_name)
    for out in tx_builder.outputs:
        for policy, names in out.amount.multi_asset.data.items():
            for name in names:
                if (bytes(policy).hex(), bytes(name).hex()) == pool_nft:
                    return out
    raise AssertionError("no pool output (carrying the pool NFT) was wired")


@pytest.mark.parametrize("supply_delta", [100_000_000, -100_000_000])
def test_lovelace_supply_pool_output_topup_withdraw(
    offline_ctx,  # noqa: ANN001
    actor_addr,  # noqa: ANN001
    topup_snap,  # noqa: ANN001
    supply_delta,  # noqa: ANN001
):
    _fix, snap = topup_snap(LOVELACE_POOL)
    # This captured pool is the only lovelace-supply market shape in the fixtures.
    assert snap.market_info.supply_token == "lovelace"
    pool = snap.pool

    tx_builder = TransactionBuilder(offline_ctx)
    build_topup_withdraw(
        tx_builder,
        snapshot=snap,
        actor_address=actor_addr,
        supply_change=supply_delta,  # positive deposits, negative withdraws
    )
    pool_out = _pool_output(tx_builder, snap)

    # The deposited/withdrawn ADA lands in (deposit) or leaves (withdraw) the pool
    # output's coin -- it is NOT dropped. The pool's pre-action lovelace dwarfs the
    # min-UTxO floor, so the floor never bites here.
    assert pool_out.amount.coin == pool.lovelace + supply_delta
    # The pre-fix reset to the pool's pre-action lovelace (`max(pool.lovelace, ...)`)
    # would have ignored the delta, so the two diverge: this is the regression the
    # invariant guards against.
    assert pool_out.amount.coin != max(pool.lovelace, OUTPUT_MIN_ADA)
