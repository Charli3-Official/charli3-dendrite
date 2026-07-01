"""Offline ledger value-preservation across the Danogo pool-spending builders.

For each captured action -- deposit, withdraw, full repay, partial repay, increase --
this reconstructs the build via the existing offline path (the structural builder +
its actor-funding helper) and asserts ledger value conservation:

    sum(input values) + mints == sum(output values) + burns,

per asset, EXCLUDING lovelace. Lovelace is excluded because the offline builder only
stubs it: the actor-funding input carries a ``PLACEHOLDER_FEE`` placeholder, the fee
is that same placeholder, and every output is floored to a min-UTxO -- the real
lovelace balancing (and the borrower's lovelace change) is the live balancer's job,
not the structural build's. After excluding lovelace, the ONLY non-zero residual is
the value the live balancer settles into the actor's change output: nothing for a
deposit/withdraw (fully self-balanced once the actor funds the supply / dTokens),
the returned owner NFT on a partial repay, the released collateral on a full repay,
and the borrowed-out supply + returned owner NFT on an increase. Each residual is
asserted EXACTLY, so a builder that created or dropped value would break the equality.

Additionally, the pool's supply-token holding delta (pool-in -> pool-out) is tied to
the action's realized supply delta. The TopupWithdraw / DecreaseLoanAmount /
IncreaseLoanAmount redeemers carry NO explicit change-amount field (the amount is
conveyed through the synthesized pool datum's ``total_supply``); the captured
``realized["pool_supply_holdings_delta"]`` (repay/increase) / ``pool_changed_amount``
(topup) is the equivalent value, so the pool holding delta is asserted against it.

Fully offline: no live backend, no balancing, no evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
from pycardano import TransactionBuilder
from pycardano import Value

from charli3_dendrite.lending.danogo.constants import PROTOCOL_CONFIG_NFT
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.transactions.build import add_actor_funding
from charli3_dendrite.lending.danogo.transactions.build import add_increase_funding
from charli3_dendrite.lending.danogo.transactions.build import add_repay_funding
from charli3_dendrite.lending.danogo.transactions.build import build_increase_loan
from charli3_dendrite.lending.danogo.transactions.build import build_repay
from charli3_dendrite.lending.danogo.transactions.build import build_topup_withdraw
from charli3_dendrite.lending.danogo.transactions.context import POOL_SCRIPT_SKH
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.danogo.transactions.context import Utxo
from charli3_dendrite.lending.danogo.transactions.context import _as_utxo

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ACTOR_UTXO = "ab" * 32 + "#0"


def _multi_asset_flat(value) -> dict[str, int]:  # noqa: ANN001
    """A pycardano `Value`'s native assets as a flat ``unit -> qty`` map."""
    out: dict[str, int] = {}
    for policy, names in value.multi_asset.data.items():
        for name, qty in names.items():
            unit = bytes(policy).hex() + bytes(name).hex()
            out[unit] = out.get(unit, 0) + qty
    return out


def _minted_value(tx_builder: TransactionBuilder) -> Value:
    """The builder's mint MultiAsset wrapped in a `Value` for flat-map reuse."""
    return Value(0, tx_builder.mint)


def _non_lovelace_residual(tx_builder: TransactionBuilder) -> dict[str, int]:
    """``inputs + mints - outputs`` per native unit (lovelace excluded), non-zero only.

    A positive residual is value the live balancer settles into the actor's change
    output; a balanced (zero) residual means that unit is fully conserved on-builder.
    """
    net: dict[str, int] = {}
    for utxo in tx_builder.inputs:
        for unit, qty in _multi_asset_flat(utxo.output.amount).items():
            net[unit] = net.get(unit, 0) + qty
    if tx_builder.mint is not None:
        for policy, names in tx_builder.mint.data.items():
            for name, qty in names.items():
                unit = bytes(policy).hex() + bytes(name).hex()
                net[unit] = net.get(unit, 0) + qty
    for out in tx_builder.outputs:
        for unit, qty in _multi_asset_flat(out.amount).items():
            net[unit] = net.get(unit, 0) - qty
    return {unit: qty for unit, qty in net.items() if qty != 0}


def _pool_output(
    tx_builder: TransactionBuilder, pool_nft: tuple[str, str]
):  # noqa: ANN202
    """The updated-pool output: the one output carrying the pool NFT."""
    for out in tx_builder.outputs:
        for policy, names in out.amount.multi_asset.data.items():
            for name in names:
                if (bytes(policy).hex(), bytes(name).hex()) == pool_nft:
                    return out
    raise AssertionError("no pool output (carrying the pool NFT) was wired")


def _supply_qty_in(pool: Utxo, supply_token: str) -> int:
    """The pool input's supply-token holding (the coin for a lovelace-supply pool)."""
    if supply_token == "lovelace":
        return pool.lovelace
    return next((q for p, n, q in pool.assets if p + n == supply_token), 0)


def _supply_qty_out(output, supply_token: str) -> int:  # noqa: ANN001
    """The pool output's supply-token holding (the coin for a lovelace-supply pool)."""
    if supply_token == "lovelace":
        return output.amount.coin
    return _multi_asset_flat(output.amount).get(supply_token, 0)


def _topup_reconstruct(
    name: str,
    *,
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type], bool],
) -> tuple[dict, TopupWithdrawSnapshot]:
    """Rebuild a `TopupWithdrawSnapshot` from an (older-style) deposit/withdraw fixture.

    These fixtures predate the top-level ``market_name`` key the conftest `topup_snap`
    factory consumes, so the market name is recovered from the single dToken mint under
    the pool script hash (mirroring the local factory in `test_build_topup_withdraw`).
    """
    fix = json.loads((FIXTURES_DIR / name).read_text())
    ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
    inputs = [_as_utxo(u) for u in fix["inputs"]]

    cfg_nft = PROTOCOL_CONFIG_NFT or ""
    protocol_config = next(u for u in ref_inputs if u.holds(cfg_nft[:56], cfg_nft[56:]))
    pd = ProtocolDatum.from_cbor(protocol_config.datum)
    pool_skh = pd.pool_skh.hex()
    config_pool_skh = pd.config_pool_skh.hex()

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
    snap = TopupWithdrawSnapshot(
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
    return fix, snap


@pytest.mark.parametrize(
    "label",
    ["deposit", "withdraw", "repay_full", "repay_partial", "increase"],
)
def test_value_preservation_invariant_offline(  # noqa: C901, PLR0915
    offline_ctx,  # noqa: ANN001
    actor_addr,  # noqa: ANN001
    repay_snap,  # noqa: ANN001
    increase_snap,  # noqa: ANN001
    script_hash,  # noqa: ANN001
    parses,  # noqa: ANN001
    label,  # noqa: ANN001
):
    tx_builder = TransactionBuilder(offline_ctx)

    if label in ("deposit", "withdraw"):
        name = "topup_tx.json" if label == "deposit" else "withdraw_tx.json"
        fix, snap = _topup_reconstruct(name, script_hash=script_hash, parses=parses)
        supply_token = snap.market_info.supply_token
        supply_change = fix["realized"]["pool_changed_amount"]

        build_topup_withdraw(
            tx_builder,
            snapshot=snap,
            actor_address=actor_addr,
            supply_change=supply_change,
        )
        dtoken = snap.pool_skh + snap.market_name
        if supply_change >= 0:  # deposit: the actor funds the deposited supply token
            funding = {supply_token: supply_change}
        else:  # withdraw: the actor funds the dTokens the builder burns
            burned = -_multi_asset_flat(_minted_value(tx_builder)).get(dtoken, 0)
            funding = {dtoken: burned}
        add_actor_funding(
            tx_builder,
            actor=actor_addr,
            actor_utxo=ACTOR_UTXO,
            funding=funding,
        )
        pool_nft = (snap.config_pool_skh, snap.market_name)
        expected_supply_delta = supply_change
        # Deposit/withdraw self-balance once the actor funds the supply / dTokens.
        expected_residual: dict[str, int] = {}

    elif label in ("repay_full", "repay_partial"):
        name = (
            "decrease_loan_tx.json"
            if label == "repay_full"
            else "decrease_loan_partial_tx.json"
        )
        fix, snap = repay_snap(name)
        supply_token = fix["supply_token"]
        amount = fix["realized"]["pool_change_amount"]

        build_repay(
            tx_builder,
            snapshot=snap,
            amount=amount,
            txn_time=fix["realized"]["txn_time"],
        )
        add_repay_funding(
            tx_builder,
            snapshot=snap,
            actor=actor_addr,
            actor_utxo=ACTOR_UTXO,
            amount=amount,
        )
        pool_nft = (snap.config_pool_skh, snap.market_name)
        # The pool's supply holding rises by the repayment net of the swept fee.
        expected_supply_delta = fix["realized"]["pool_supply_holdings_delta"]
        loan_token = snap.loan_skh + snap.market_name
        if fix["variant"] == "full":
            # A full repay burns the loan token + owner NFT (both conserved via the
            # mint) and releases the loan's collateral to the borrower -- the released
            # collateral is the live balancer's change leg.
            expected_residual = {
                p + n: q for p, n, q in snap.loan.assets if p + n != loan_token
            }
        else:
            # A partial repay keeps the loan open: the owner NFT the borrower supplied
            # (proof of ownership) is returned to them by balancing.
            expected_residual = {snap.owner_nft: 1}

    elif label == "increase":
        fix, snap = increase_snap("increase_loan_tx.json")
        supply_token = fix["supply_token"]
        pool_changed_amount = fix["realized"]["pool_changed_amount"]

        build_increase_loan(
            tx_builder,
            snapshot=snap,
            borrow_amount=-pool_changed_amount,
            txn_time=fix["realized"]["txn_time"],
        )
        add_increase_funding(
            tx_builder,
            snapshot=snap,
            actor=actor_addr,
            actor_utxo=ACTOR_UTXO,
        )
        pool_nft = (snap.config_pool_skh, snap.market_name)
        expected_supply_delta = fix["realized"]["pool_supply_holdings_delta"]
        # The pool pays the borrowed supply OUT (it lands with the borrower via
        # balancing), and the owner NFT the borrower supplied is returned to them.
        expected_residual = {supply_token: -pool_changed_amount, snap.owner_nft: 1}

    else:  # pragma: no cover - guard against a new label that forgets a branch
        raise AssertionError(f"unhandled action label {label!r}")

    # 1) Ledger value conservation (lovelace excluded): the only non-zero residual is
    # the exact value the live balancer settles into the actor's change output.
    assert _non_lovelace_residual(tx_builder) == expected_residual, label

    # 2) The pool's supply-token holding delta matches the action's realized delta.
    pool_in = _supply_qty_in(snap.pool, supply_token)
    pool_out = _supply_qty_out(_pool_output(tx_builder, pool_nft), supply_token)
    assert pool_out - pool_in == expected_supply_delta, label
