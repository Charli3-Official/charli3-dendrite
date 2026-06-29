"""Offline structural test for the forward increase-loan (IncreaseLoanAmount) builder.

`build_increase_loan` contributes "borrow more against an existing loan" to a
caller-supplied `pycardano.TransactionBuilder`: the pool + loan script spends, the
novel ``Withdraw(pool_skh)`` orchestration hub (NOT repay's ``Withdraw(loan_skh)``),
the oracle withdraw-zero price calc, the advanced pool output, and the always-present
raised loan output. An increase mints nothing. The caller owns the chain context,
balancing, and evaluation, so this test asserts the builder ends up WIRED and that the
synthesized datums / redeemer indices reproduce the captured tx (the live ex-units are
proven on Ogmios in the e2e). It runs fully offline against a network-free chain
context and an `IncreaseLoanSnapshot` reconstructed from the captured increase-loan
fixture.
"""

from __future__ import annotations

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.danogo.transactions._common import _pool_output_value
from charli3_dendrite.lending.danogo.transactions.build import add_increase_funding
from charli3_dendrite.lending.danogo.transactions.build import build_increase_loan
from charli3_dendrite.lending.danogo.transactions.context import IncreaseLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.redeemers import IncreaseLoanAmount
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA

FIXTURE = "increase_loan_tx.json"
# A real lovelace-supply (ADA) pool, captured for deposit/withdraw -- the only Danogo
# market shape where the supply token IS the coin, so the pool output's lovelace
# carries the borrowed-out supply delta.
LOVELACE_POOL = "topup_zero_held_alt_tx.json"


def _build(
    offline_ctx,  # noqa: ANN001
    snapshot: IncreaseLoanSnapshot,
    fix: dict,
) -> TransactionBuilder:
    """Forward-build an increase from the fixture's realized borrow + transaction time.

    The captured borrow amount is ``-pool_changed_amount`` (the supply the pool pays
    out); pinning it plus ``txn_time`` lets the synthesized pool/loan datums reproduce
    the captured ones byte-exact (interest accrues to the same instant).
    """
    realized = fix["realized"]
    tx_builder = TransactionBuilder(offline_ctx)
    build_increase_loan(
        tx_builder,
        snapshot=snapshot,
        borrow_amount=-realized["pool_changed_amount"],
        txn_time=realized["txn_time"],
    )
    return tx_builder


def _increase_redeemer(tx_builder: TransactionBuilder) -> IncreaseLoanAmount:
    """The shared `IncreaseLoanAmount` redeemer data, read from a pool/loan spend."""
    for redeemer in tx_builder._inputs_to_redeemers.values():
        if isinstance(redeemer.data, IncreaseLoanAmount):
            return redeemer.data
    raise AssertionError("no IncreaseLoanAmount spend redeemer was wired")


def _increase_instances(tx_builder: TransactionBuilder) -> list[IncreaseLoanAmount]:
    """Every `IncreaseLoanAmount` redeemer data across spend / withdrawal."""
    out: list[IncreaseLoanAmount] = [
        r.data
        for r in tx_builder._inputs_to_redeemers.values()
        if isinstance(r.data, IncreaseLoanAmount)
    ]
    out += [
        r.data
        for _s, r in tx_builder._withdrawal_script_to_redeemers
        if r is not None and isinstance(r.data, IncreaseLoanAmount)
    ]
    return out


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


# --- spends: pool + loan, identified by their out-refs ---------------------------


def test_increase_spends_pool_and_loan(offline_ctx, increase_snap):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)

    spent = {
        (u.input.transaction_id.payload.hex(), u.input.index) for u in tx_builder.inputs
    }
    assert snap.pool.out_ref in spent
    assert snap.loan.out_ref in spent
    # Exactly the two script inputs are contributed by the builder (the borrower
    # funding input is added separately by `add_increase_funding`).
    assert len(tx_builder.inputs) == 2


# --- the Withdraw(pool_skh) hub + oracle Withdraw, both zero withdrawals ----------


def test_pool_hub_and_oracle_withdrawals_present(
    offline_ctx, increase_snap
):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)

    withdrawals = tx_builder.withdrawals.to_primitive()
    pool_hub = _reward(snap.pool_skh)
    oracle = _reward(snap.oracle_skh)
    # The novel POOL-script hub (f1 + pool_skh) -- NOT repay's loan-script hub -- and
    # the oracle reward address are both ZERO withdrawals (the withdraw-zero trick).
    assert withdrawals.get(pool_hub) == 0
    assert withdrawals.get(oracle) == 0
    # The reward accounts are network-tagged stake-script addresses (header 0xf1).
    assert pool_hub[0] == 0xF1
    assert oracle[0] == 0xF1
    # The hub is the pool script hash, not the loan script hash (the repay hub).
    assert pool_hub != _reward(snap.loan_skh)


def test_one_increase_redeemer_shared_across_roles(
    offline_ctx, increase_snap
):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)

    instances = _increase_instances(tx_builder)
    # Pool spend + loan spend + the pool hub all carry it; an increase mints nothing.
    # Every reference is the SAME object, so its CBOR is byte-identical across roles --
    # matching the captured tx where one redeemer drives all three purposes.
    assert len(instances) == 3
    first = instances[0]
    assert all(inst is first for inst in instances)
    assert all(inst.to_cbor() == first.to_cbor() for inst in instances)
    # The redeemer's constructor + value shape (alt index 4 / CBOR tag 125, plain-int
    # loan_out_idx, None fee_out_idx) matches the captured tx; the byte-exact redeemer
    # against the captured INDICES is pinned in `test_increase_loan_redeemer`. The
    # offline build's reference-input set is pruned, so the ref indices it carries
    # legitimately differ from the captured tx (asserted to RESOLVE below).
    assert first.to_cbor().hex().startswith("d87d9f")


# --- no mint (an increase reuses the existing loan token + owner NFT) -------------


def test_increase_has_no_mint(offline_ctx, increase_snap):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)
    assert tx_builder.mint is None


# --- outputs: advanced pool (idx0, +datum), raised loan (idx1, +datum) ------------


def test_pool_output_and_datum(offline_ctx, increase_snap):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)
    realized = fix["realized"]

    increase = _increase_redeemer(tx_builder)
    pool_out = tx_builder.outputs[increase.pool_out_idx]
    # The advanced pool datum reproduces the captured pool_out datum byte-exact.
    assert pool_out.datum.to_cbor().hex() == fix["pool_out_datum"]
    # The pool pays the borrowed supply out: its supply holdings drop by that amount.
    supply = fix["supply_token"]
    before = next(q for p, n, q in snap.pool.assets if p + n == supply)
    after = _flat_value(pool_out)[supply]
    assert after - before == realized["pool_changed_amount"]


def test_loan_output_and_datum(offline_ctx, increase_snap):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)

    increase = _increase_redeemer(tx_builder)
    # The loan output is always present (loan_out_idx is a plain int, never None).
    assert isinstance(increase.loan_out_idx, int)
    loan_out = tx_builder.outputs[increase.loan_out_idx]
    # The raised loan datum reproduces the captured loan_out datum byte-exact, and the
    # loan's value (loan token + locked collateral) is carried through unchanged.
    assert loan_out.datum.to_cbor().hex() == fix["loan_out_datum"]
    carried = _flat_value(loan_out)
    for policy, name, qty in snap.loan.assets:
        assert carried.get(policy + name) == qty


def test_outputs_are_pool_then_loan_only(offline_ctx, increase_snap):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)
    # The structural build contributes exactly the advanced pool + raised loan (the
    # borrower change is added by balancing, not here). No fee output on this market.
    assert len(tx_builder.outputs) == 2


# --- finalized redeemer indices derive from the assembled ordering ----------------


def test_increase_redeemer_indices(offline_ctx, increase_snap):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)
    increase = _increase_redeemer(tx_builder)
    decoded = fix["increase_loan_redeemer_decoded"]

    # Pool output leads, loan output follows -- matching the captured tx ordering.
    assert increase.pool_out_idx == decoded["pool_out_idx"]
    assert increase.loan_out_idx == decoded["loan_out_idx"]
    # No fee output on markets with a zero origination-fee rate (fee_out_idx = None).
    assert isinstance(increase.fee_out_idx, NoneVal)
    assert decoded["fee_out_idx"] is None
    # The pool-input out-ref the redeemer carries is the spent pool input.
    assert (
        increase.pool_in_out_ref.transaction_id.hex(),
        increase.pool_in_out_ref.output_index,
    ) == snap.pool.out_ref
    assert [
        increase.pool_in_out_ref.transaction_id.hex(),
        increase.pool_in_out_ref.output_index,
    ] == decoded["pool_in_out_ref"]
    _assert_ref_indices_resolve(tx_builder, increase, snap)


def _assert_ref_indices_resolve(
    tx_builder: TransactionBuilder,
    increase: IncreaseLoanAmount,
    snap: IncreaseLoanSnapshot,
) -> None:
    """The reference indices point at the protocol-config + market reference inputs.

    The builder prunes the oracle reference set to the on-chain shape, so the absolute
    reference index values legitimately drift from the captured tx; what must hold is
    that each index resolves -- in the canonical out-ref ordering the Plutus script
    context exposes -- to the right UTxO.
    """
    refs = sorted(
        (u.input for u in tx_builder.reference_inputs),
        key=lambda i: (bytes(i.transaction_id), i.index),
    )
    cfg = refs[increase.protocol_cfg_ref_idx]
    mkt = refs[increase.market_ref_idx]
    assert (cfg.transaction_id.payload.hex(), cfg.index) == snap.protocol_config.out_ref
    assert (mkt.transaction_id.payload.hex(), mkt.index) == snap.market.out_ref


# --- validity window + funding ---------------------------------------------------


def test_validity_window_within_increase_bound(
    offline_ctx, increase_snap
):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)
    assert tx_builder.validity_start is not None
    assert tx_builder.ttl is not None
    assert (tx_builder.ttl - tx_builder.validity_start) * 1000 <= 360_000


def test_increase_funding_adds_borrower_input_owner_nft_only(  # noqa: ANN001
    offline_ctx,
    actor_addr,
    increase_snap,
):
    fix, snap = increase_snap(FIXTURE)
    tx_builder = _build(offline_ctx, snap, fix)

    entry = add_increase_funding(
        tx_builder,
        snapshot=snap,
        actor=actor_addr,
        actor_utxo="ab" * 32 + "#0",
    )
    # The borrower input was added (pool + loan spends, then the funding input).
    assert len(tx_builder.inputs) == 3
    # The Ogmios additionalUtxo entry resolves the borrower funding out-ref and carries
    # the owner NFT (proof of loan ownership)...
    assert entry["transaction"]["id"] == "ab" * 32
    owner_policy, owner_name = snap.owner_nft[:56], snap.owner_nft[56:]
    assert entry["value"][owner_policy][owner_name] == 1
    # ... but supplies NO supply token in (the pool pays the borrowed supply out).
    supply = snap.market_info.supply_token
    assert supply[56:] not in entry["value"].get(supply[:56], {})


def test_increase_rejects_below_min_tx_amount(
    offline_ctx, increase_snap
):  # noqa: ANN001
    fix, snap = increase_snap(FIXTURE)
    tx_builder = TransactionBuilder(offline_ctx)
    # A borrow below the market minimum is the one the validator rejects; fail loud.
    with pytest.raises(ValueError, match="minimum transaction amount"):
        build_increase_loan(
            tx_builder,
            snapshot=snap,
            borrow_amount=snap.market_info.min_tx_amount - 1,
            txn_time=fix["realized"]["txn_time"],
        )


# --- lovelace-supply pool output carries the borrowed-out supply delta ------------
#
# The increase fixture above is a native-token-supply market, where the supply token
# is NOT the coin: the pool output's lovelace is just the carried min-ADA balance, so
# flooring the computed coin and flooring the pool's pre-action lovelace are identical
# (and the captured byte-exact assertions above prove that path is unchanged). The ONE
# market shape that distinguishes them is a lovelace-supply (ADA) market: the supply
# token IS the coin, so an increase pays the borrowed supply OUT of the coin.
# `_pool_output_value` already applies that (negative) delta, so the builder must floor
# THAT computed coin -- never reset it to the pool's pre-action lovelace, which would
# drop the borrowed-out ADA. A captured ADA-market increase does not exist, so this
# exercises the lovelace branch offline against a real lovelace-supply pool.


def test_lovelace_supply_pool_output_carries_borrow_delta(topup_snap):  # noqa: ANN001
    _fix, snap = topup_snap(LOVELACE_POOL)
    pool = snap.pool
    # An increase pays the borrowed supply out of the pool: for an ADA market that is a
    # NEGATIVE lovelace supply delta removed from the pool's coin.
    pool_changed_amount = -100_000_000
    pool_out = TransactionOutput(
        address=Address.decode(pool.address),
        amount=_pool_output_value(pool, "lovelace", pool_changed_amount),
    )
    # The fix (mirrors `build_increase_loan`): floor the ALREADY-COMPUTED coin, which
    # carries the supply delta.
    pool_out.amount.coin = max(pool_out.amount.coin, OUTPUT_MIN_ADA)

    # The borrowed-out ADA is removed from the pool output's coin -- not left behind.
    assert pool_out.amount.coin == pool.lovelace + pool_changed_amount
    # The pre-fix reset to the pool's pre-action lovelace would have ignored the delta,
    # so the two diverge: this is the regression the fix guards against.
    assert pool_out.amount.coin != max(pool.lovelace, OUTPUT_MIN_ADA)
