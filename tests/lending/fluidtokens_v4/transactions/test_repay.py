"""Offline: forward-built V4 repays against the captured mainnet repays."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanRepaymentData
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanRepayActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import build_repay
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import at_minimum_ada
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_output_view
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import output_view
from tests.lending.fluidtokens_v4.transactions.replay import redeemers
from tests.lending.fluidtokens_v4.transactions.replay import reference_script


def _outputs(fix: dict, count: int) -> tuple[list, list]:
    snapshot = RepaySnapshot.from_capture(fix)
    body = build(build_repay, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    return (
        [output_view(o) for o in body.outputs[:count]],
        [captured_output_view(o) for o in fix["outputs"][:count]],
    )


def test_single_repay_is_byte_exact() -> None:
    fix = fixture("repay_single")
    built = build(
        build_repay, RepaySnapshot.from_capture(fix), slot=fix["invalid_before"]
    )
    assert redeemers(built.tx) == captured_redeemers(fix)
    # The lender's payment and the returned bond; the wallet change follows.
    built_outputs, captured = _outputs(fix, 2)
    assert built_outputs == captured


def test_multi_repay_matches_but_for_the_unread_burn_origin() -> None:
    fix = fixture("repay_multi")
    built = redeemers(
        build(
            build_repay, RepaySnapshot.from_capture(fix), slot=fix["invalid_before"]
        ).tx,
    )
    captured = captured_redeemers(fix)
    assert {r for r in built if r[0] != "mint"} == {
        r for r in captured if r[0] != "mint"
    }
    # A burn-only loan mint never reads its origin index. FluidTokens writes 6; the
    # builder points it at the loan dispatch withdraw, the 4th redeemer.
    (built_mint,) = [r for r in built if r[0] == "mint"]
    (captured_mint,) = [r for r in captured if r[0] == "mint"]
    for mint, origin in ((captured_mint, 6), (built_mint, 4)):
        redeemer = LoanMintRedeemer.from_cbor(mint[2])
        assert redeemer.config_ref_input_index == 1
        assert redeemer.origin_withdraw_redeemer_index == origin
    built_outputs, captured_outputs = _outputs(fix, 6)
    assert built_outputs == captured_outputs


def test_repay_pays_the_lender_at_the_asset_manager() -> None:
    fix = fixture("repay_single")
    snapshot = RepaySnapshot.from_capture(fix)
    (position,) = snapshot.positions
    payment = build(
        build_repay, snapshot, slot=fix["invalid_before"]
    ).tx.transaction_body.outputs[0]
    assert payment.address.payment_part.payload.hex() == c.ASSET_MANAGER_SPEND_SKH
    assert (
        payment.address.staking_part
        == Address.decode(position.loan.address).staking_part
    )
    datum = AssetManagerDatumWithToken.from_cbor(payment.datum.to_cbor())
    assert datum.action == b"installment_repayment"
    assert datum.owner_asset.policy_id.hex() == c.LENDER_BOND_POLICY
    assert datum.owner_asset.asset_name == position.loan_id
    data = LoanRepaymentData.from_cbor(datum.data.to_cbor())
    assert (data.loan_id, data.repaid_installments) == (position.loan_id, 1)


def _with_loan(snapshot: RepaySnapshot, **changes: object) -> RepaySnapshot:
    """``snapshot`` with its one loan's datum changed."""
    (position,) = snapshot.positions
    datum = replace(position.loan_datum, **changes)
    position.loan = replace(position.loan, datum=datum.to_cbor_hex())
    position.lender_lovelace = None
    return snapshot


def test_an_installment_repay_continues_the_loan() -> None:
    fix = fixture("repay_single")
    snapshot = _with_loan(RepaySnapshot.from_capture(fix), installment_period=720)
    (position,) = snapshot.positions
    position.is_final = False
    position.payment = 74_302
    body = build(build_repay, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    assert body.mint is None
    loan = body.outputs[0]
    assert str(loan.address) == position.loan.address
    assert LoanDatum.from_cbor(loan.datum.to_cbor()).repaid_installments == 1
    # The lender is paid the installment, not less than an output's minimum ADA.
    assert body.outputs[1].amount.coin >= 74_302


def test_a_receipt_loan_mints_its_receipt_into_the_payment() -> None:
    fix = fixture("repay_single")
    snapshot = _with_loan(
        RepaySnapshot.from_capture(fix), repayment_receipts=BoolTrue()
    )
    snapshot.asset_manager_policy_script_ref = reference_script("repayment_policy")
    (position,) = snapshot.positions
    body = build(build_repay, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    receipt = {
        (bytes(p).hex(), n.payload.hex()): q
        for p, names in body.mint.items()
        for n, q in names.items()
        if bytes(p).hex() == c.REPAYMENT_POLICY
    }
    assert receipt == {(c.REPAYMENT_POLICY, position.receipt_name.hex()): 1}
    payment = body.outputs[0]
    assert payment.amount.multi_asset[
        next(
            p
            for p in payment.amount.multi_asset
            if bytes(p).hex() == c.REPAYMENT_POLICY
        )
    ]


def test_a_receipt_loan_needs_the_receipt_policy() -> None:
    fix = fixture("repay_single")
    snapshot = _with_loan(
        RepaySnapshot.from_capture(fix), repayment_receipts=BoolTrue()
    )
    with pytest.raises(ValueError, match="asset-manager policy"):
        build(build_repay, snapshot, slot=fix["invalid_before"])


def test_open_loans_must_sort_before_closed_ones() -> None:
    fix = fixture("repay_multi")
    snapshot = RepaySnapshot.from_capture(fix)
    last = snapshot.ordered_positions[-1]
    datum = replace(last.loan_datum, installment_period=720)
    last.loan = replace(last.loan, datum=datum.to_cbor_hex())
    last.is_final = False
    with pytest.raises(ValueError, match="sort before"):
        build(build_repay, snapshot, slot=fix["invalid_before"])


def test_a_loan_action_must_own_the_loan_script_outputs() -> None:
    fix = fixture("repay_single")
    snapshot = RepaySnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.positions[0].loan.address), 5_000_000
        ),
    )
    with pytest.raises(ValueError, match="only payer"):
        build_repay(tx_builder, snapshot=snapshot)


def _one_bond_utxo(snapshot: RepaySnapshot) -> Utxo:
    """Every loan's bond gathered into one wallet UTxO, as a consolidating wallet does."""
    first = snapshot.positions[0].borrower_bond
    shared = replace(
        first,
        out_ref=("bb" * 32, 0),
        assets=[a for p in snapshot.positions for a in p.borrower_bond.assets],
        datum=None,
    )
    for position in snapshot.positions:
        position.borrower_bond = shared
    return shared


def test_loans_sharing_one_bond_utxo_return_it_once() -> None:
    fix = fixture("repay_multi")
    snapshot = RepaySnapshot.from_capture(fix)
    shared = _one_bond_utxo(snapshot)
    built = build(build_repay, snapshot, slot=fix["invalid_before"])
    outputs = built.tx.transaction_body.outputs
    returned = [i for i, o in enumerate(outputs) if str(o.address) == shared.address]
    assert returned == [3]  # after the three lender payments
    (action,) = [
        LoanRepayActionWithdrawRedeemer.from_cbor(cbor)
        for purpose, index, cbor in redeemers(built.tx)
        if (purpose, index) == ("reward", 1)
    ]
    assert [d.borrower_bond_output_index for d in action.actions_for_each_input] == [
        3,
        3,
        3,
    ]


def test_a_bond_held_by_a_script_is_refused() -> None:
    fix = fixture("repay_single")
    snapshot = RepaySnapshot.from_capture(fix)
    (position,) = snapshot.positions
    position.borrower_bond = replace(
        position.borrower_bond,
        address=position.loan.address,
    )
    with pytest.raises(NotImplementedError, match="wallet-held"):
        build(build_repay, snapshot, slot=fix["invalid_before"])


def test_funding_repeating_a_spent_loan_or_bond_adds_it_once() -> None:
    fix = fixture("repay_single")
    snapshot = RepaySnapshot.from_capture(fix)
    (position,) = snapshot.positions
    funding = list(snapshot.funding)
    snapshot.funding = [*funding, position.loan, position.borrower_bond]
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_repay(tx_builder, snapshot=snapshot)
    spent = [
        (bytes(u.input.transaction_id).hex(), u.input.index) for u in tx_builder.inputs
    ]
    assert spent.count(position.out_ref) == 1
    assert spent.count(position.borrower_bond.out_ref) == 1
    # The bond and the original funding, each recorded once for Ogmios.
    assert len(snapshot.additional_utxo()) == len(funding) + 1


def test_a_continuing_repay_needs_the_loan_ada_to_cover_its_output() -> None:
    fix = fixture("repay_single")
    snapshot = _with_loan(
        RepaySnapshot.from_capture(fix),
        installment_period=720,
        repaid_installments=23,
    )
    (position,) = snapshot.positions
    position.is_final = False
    position.payment = 74_302
    # Exactly enough for the loan as it stands; the 24th installment adds a byte.
    position.loan = at_minimum_ada(position.loan)
    with pytest.raises(ValueError, match="requires the loan's value unchanged"):
        build(build_repay, snapshot, slot=fix["invalid_before"])
    position.loan = replace(position.loan, lovelace=position.loan.lovelace + 4_310)
    body = build(build_repay, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    assert body.outputs[0].amount.coin == min_ada(body.outputs[0])
