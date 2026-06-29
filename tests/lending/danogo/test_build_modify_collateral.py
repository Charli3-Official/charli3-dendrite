"""Offline structural test for the forward modify-collateral (ModifyCollaterals) builder.

`build_modify_collateral` contributes "add and/or remove collateral on an existing loan,
without repaying" to a caller-supplied `pycardano.TransactionBuilder`: the loan script
spend, the oracle withdraw-zero price calc (the ONLY withdrawal -- no hub), the
protocol-config / market / pool / oracle reference inputs (the pool is a REFERENCE
input, NEVER spent), and the continuing loan output reusing the input loan datum
VERBATIM with the TARGET collateral locked. A modify mints nothing and spends no pool.

The caller owns the chain context, balancing, and evaluation, so this test asserts the
builder ends up WIRED and that the redeemer indices reproduce the captured tx (the live
ex-units are proven on Ogmios in the e2e). It runs fully offline against a network-free
chain context and a `ModifyCollateralSnapshot` reconstructed from each captured fixture.

To keep the structural assertions independent of oracle price values, every fixture is
built here with its captured oracle redeemer (the builder's documented override); the
wiring asserted -- the spend, the reference set, the loan output, and the redeemer
indices -- is identical regardless of how the oracle prices resolve. Forward oracle
synthesis is proven separately on Ogmios in the e2e: all three fixtures forward-synthesize
the oracle price calc (prices + referenced leaf set) from live source leaves.
"""

from __future__ import annotations

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash
from pycardano import TransactionBuilder

from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.transactions.build import (
    add_modify_collateral_funding,
)
from charli3_dendrite.lending.danogo.transactions.build import build_modify_collateral
from charli3_dendrite.lending.danogo.transactions.context import (
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.danogo.transactions.redeemers import ModifyCollaterals

FIXTURES = [
    "modify_collateral_add_tx.json",
    "modify_collateral_remove_tx.json",
    "modify_collateral_swap_tx.json",
]


def _target(fix: dict) -> dict[str, int]:
    """The absolute TARGET collateral the loan output should lock (unit -> qty)."""
    return {unit: int(qty) for unit, qty in fix["collateral"]["collateral_out"].items()}


def _build(
    offline_ctx,  # noqa: ANN001
    snapshot: ModifyCollateralSnapshot,
    fix: dict,
) -> TransactionBuilder:
    """Forward-build a modify from the fixture, replaying its captured oracle redeemer.

    The captured oracle redeemer is replayed only to keep these structural assertions
    independent of oracle price values; the spend, the reference set, the loan output,
    and every redeemer index are forward-synthesized from the snapshot + target
    collateral. (Forward oracle synthesis itself is proven on Ogmios in the e2e.)
    """
    tx_builder = TransactionBuilder(offline_ctx)
    build_modify_collateral(
        tx_builder,
        snapshot=snapshot,
        target_collateral=_target(fix),
        txn_time=fix["block_time"] * 1000,
        oracle_redeemer=OraclePriceCalcRdmr.from_cbor(fix["oracle_redeemer"]),
    )
    return tx_builder


def _modify_redeemer(tx_builder: TransactionBuilder) -> ModifyCollaterals:
    """The `ModifyCollaterals` redeemer data, read from the sole loan spend."""
    for redeemer in tx_builder._inputs_to_redeemers.values():
        if isinstance(redeemer.data, ModifyCollaterals):
            return redeemer.data
    raise AssertionError("no ModifyCollaterals spend redeemer was wired")


def _flat_value(output) -> dict[str, int]:  # noqa: ANN001
    """A TransactionOutput's native assets as a flat ``unit -> qty`` map."""
    return {
        bytes(policy).hex() + bytes(name).hex(): qty
        for policy, names in output.amount.multi_asset.data.items()
        for name, qty in names.items()
    }


def _reward(skh: str) -> bytes:
    return bytes(
        Address(staking_part=ScriptHash(bytes.fromhex(skh)), network=Network.MAINNET),
    )


# --- spend: ONLY the loan; the pool is a reference, never spent -------------------


@pytest.mark.parametrize("fixture", FIXTURES)
def test_modify_spends_loan_only_pool_is_reference(
    offline_ctx,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)

    spent = {
        (u.input.transaction_id.payload.hex(), u.input.index) for u in tx_builder.inputs
    }
    ref = {
        (u.input.transaction_id.payload.hex(), u.input.index)
        for u in tx_builder.reference_inputs
    }
    # The loan is spent; the pool is a reference input that is NEVER spent.
    assert snap.loan.out_ref in spent
    assert snap.pool.out_ref not in spent
    assert snap.pool.out_ref in ref
    # Exactly the single loan script input is contributed by the builder (the borrower
    # funding input is added separately by `add_modify_collateral_funding`).
    assert len(tx_builder.inputs) == 1


# --- the oracle Withdraw is the ONLY withdrawal (no hub) --------------------------


@pytest.mark.parametrize("fixture", FIXTURES)
def test_only_oracle_withdrawal_no_hub(
    offline_ctx,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)

    withdrawals = tx_builder.withdrawals.to_primitive()
    oracle = _reward(snap.oracle_skh)
    # The oracle reward address is the ONLY (zero) withdrawal -- a modify has no loan
    # hub (repay's `Withdraw(loan_skh)`) and no pool hub (increase's `Withdraw(pool_skh)`).
    assert set(withdrawals) == {oracle}
    assert withdrawals[oracle] == 0
    assert oracle[0] == 0xF1
    assert _reward(snap.loan_skh) not in withdrawals
    assert _reward(snap.pool_skh) not in withdrawals


# --- no mint (a modify only moves locked collateral) ------------------------------


@pytest.mark.parametrize("fixture", FIXTURES)
def test_modify_has_no_mint(
    offline_ctx,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)
    assert tx_builder.mint is None


# --- loan output: datum passthrough + TARGET collateral (idx0) --------------------


@pytest.mark.parametrize("fixture", FIXTURES)
def test_loan_output_datum_passthrough_and_target_collateral(
    offline_ctx,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)

    modify = _modify_redeemer(tx_builder)
    loan_out = tx_builder.outputs[modify.loan_out_idx]
    # The loan datum is byte-unchanged in -> out: the output reuses the input datum
    # verbatim (and the captured tx's loan_in / loan_out datums are identical).
    assert fix["loan_in_datum"] == fix["loan_out_datum"]
    assert loan_out.datum.to_cbor().hex() == fix["loan_in_datum"]
    # The loan output locks exactly the TARGET collateral, plus the loan token (qty 1).
    carried = _flat_value(loan_out)
    loan_token = snap.loan_skh + snap.market_name
    assert carried.get(loan_token) == 1
    collateral = {u: q for u, q in carried.items() if u != loan_token}
    assert collateral == _target(fix)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_single_output_is_the_loan(
    offline_ctx,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)
    # The structural build contributes exactly the continuing loan (the borrower change
    # is added by balancing, not here). No pool output, no fee output.
    assert len(tx_builder.outputs) == 1


# --- finalized redeemer indices reproduce the captured tx -------------------------


@pytest.mark.parametrize("fixture", FIXTURES)
def test_modify_redeemer_indices(
    offline_ctx,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)

    modify = _modify_redeemer(tx_builder)
    decoded = fix["modify_collateral_redeemer_decoded"]
    # Reproducing the FULL captured oracle reference set (the override path) keeps the
    # reference ordering identical to the captured tx, so the four index fields match
    # the captured redeemer exactly -- including the swap fixture's multi-pool ordering.
    assert modify.loan_out_idx == decoded["loan_out_idx"]
    assert modify.protocol_cfg_ref_idx == decoded["protocol_cfg_ref_idx"]
    assert modify.market_ref_idx == decoded["market_ref_idx"]
    assert modify.pool_ref_idx == decoded["pool_ref_idx"]
    _assert_ref_indices_resolve(tx_builder, modify, snap)


def _assert_ref_indices_resolve(
    tx_builder: TransactionBuilder,
    modify: ModifyCollaterals,
    snap: ModifyCollateralSnapshot,
) -> None:
    """Each reference index resolves -- in canonical out-ref order -- to its UTxO."""
    refs = sorted(
        (u.input for u in tx_builder.reference_inputs),
        key=lambda i: (bytes(i.transaction_id), i.index),
    )
    cfg = refs[modify.protocol_cfg_ref_idx]
    mkt = refs[modify.market_ref_idx]
    pool = refs[modify.pool_ref_idx]
    assert (cfg.transaction_id.payload.hex(), cfg.index) == snap.protocol_config.out_ref
    assert (mkt.transaction_id.payload.hex(), mkt.index) == snap.market.out_ref
    assert (pool.transaction_id.payload.hex(), pool.index) == snap.pool.out_ref


# --- validity window + funding ---------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURES)
def test_validity_window_within_modify_bound(
    offline_ctx,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)
    assert tx_builder.validity_start is not None
    assert tx_builder.ttl is not None
    assert (tx_builder.ttl - tx_builder.validity_start) * 1000 <= 360_000


@pytest.mark.parametrize("fixture", FIXTURES)
def test_modify_funding_adds_borrower_input_with_owner_nft(
    offline_ctx,  # noqa: ANN001
    actor_addr,  # noqa: ANN001
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    fix, snap = modify_snap(fixture)
    tx_builder = _build(offline_ctx, snap, fix)

    entry = add_modify_collateral_funding(
        tx_builder,
        snapshot=snap,
        actor=actor_addr,
        actor_utxo="ab" * 32 + "#0",
        target_collateral=_target(fix),
    )
    # The borrower input was added (the loan spend, then the funding input).
    assert len(tx_builder.inputs) == 2
    # The Ogmios additionalUtxo entry resolves the borrower funding out-ref and carries
    # the owner NFT (qty 1, proof of loan ownership; nothing is burned).
    assert entry["transaction"]["id"] == "ab" * 32
    owner_policy, owner_name = snap.owner_nft[:56], snap.owner_nft[56:]
    assert entry["value"][owner_policy][owner_name] == 1
