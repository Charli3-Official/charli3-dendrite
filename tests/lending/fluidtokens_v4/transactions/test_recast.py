"""Offline: the shape of a forward-built V4 recast."""

from __future__ import annotations

from dataclasses import replace

import pytest

from charli3_dendrite.lending.fluidtokens import math as finance
from charli3_dendrite.lending.fluidtokens.datums import InterestOnRemainingPrincipal
from charli3_dendrite.lending.fluidtokens.datums import PerpetualLoan
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    remaining_debt,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import RecastSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import build_recast
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanRecastActionWithdrawRedeemer,
)
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.fluidtokens_v4.transactions.recast_setup import recast_batch_snapshot
from tests.lending.fluidtokens_v4.transactions.recast_setup import recast_snapshot
from tests.lending.fluidtokens_v4.transactions.replay import at_minimum_ada
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import redeemers


def test_a_partial_recast_continues_the_loan() -> None:
    snapshot, _, slot = recast_snapshot(amount_paid=10_000_000)
    (position,) = snapshot.positions
    body = build(build_recast, snapshot, slot=slot).tx.transaction_body
    assert body.mint is None
    loan, payment, bond = body.outputs[:3]
    assert str(loan.address) == position.loan.address
    datum = LoanDatum.from_cbor(loan.datum.to_cbor())
    assert (datum.done_recasts, datum.principal_amount) == (1, 10_000_004)
    receipt = AssetManagerDatumWithToken.from_cbor(payment.datum.to_cbor())
    assert (receipt.action, receipt.data) == (b"recast", position.loan_id)
    assert payment.amount.coin == 10_000_000
    assert str(bond.address) == position.borrower_bond.address


def test_recast_redeemer_points_at_the_bond() -> None:
    snapshot, _, slot = recast_snapshot(amount_paid=10_000_000)
    built = build(build_recast, snapshot, slot=slot)
    # Withdraw redeemers follow their script hashes: the loan dispatch, then recast.
    assert c.LOAN_POLICY < c.LOAN_RECAST_ACTION_SKH
    (cbor,) = [
        cbor
        for purpose, i, cbor in redeemers(built.tx)
        if (purpose, i) == ("reward", 1)
    ]
    (data,) = LoanRecastActionWithdrawRedeemer.from_cbor(cbor).actions_for_each_input
    assert (data.borrower_bond_output_index, data.amount_paid) == (2, 10_000_000)


def test_paying_the_whole_debt_closes_the_loan() -> None:
    snapshot, _, slot = recast_snapshot(amount_paid=0)
    (position,) = snapshot.positions
    position.amount_paid = remaining_debt(
        position.loan_datum,
        valid_to_ms=slot_to_posix_ms(snapshot.valid_to),
    )
    body = build(build_recast, snapshot, slot=slot).tx.transaction_body
    burned = {
        n.payload: q
        for p, names in body.mint.items()
        for n, q in names.items()
        if bytes(p).hex() == c.LOAN_POLICY
    }
    assert burned == {position.loan_id: -1}
    payment = body.outputs[0]
    assert payment.address.payment_part.payload.hex() == c.ASSET_MANAGER_SPEND_SKH


def test_a_recast_the_contract_rejects_raises() -> None:
    snapshot, _, slot = recast_snapshot(amount_paid=1)
    with pytest.raises(ValueError, match="more than the interest"):
        build(build_recast, snapshot, slot=slot)


def _resolve_from(monkeypatch: pytest.MonkeyPatch, snapshot: RecastSnapshot) -> None:
    """Resolve the snapshot's loan, config and scripts for ``from_backend``."""
    (position,) = snapshot.positions
    context = resolve.LoanActionContext(
        positions=[position],
        funding=snapshot.funding,
        config=snapshot.config,
        scripts=resolve.config_datum(snapshot.config),
        loan_spend_script_ref=snapshot.loan_spend_script_ref,
        loan_policy_script_ref=snapshot.loan_policy_script_ref,
        action_script_ref=snapshot.action_script_ref,
    )
    monkeypatch.setattr(resolve, "resolve_loan_action", lambda _backend, **_: context)


def test_from_backend_refuses_while_the_reward_account_is_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _, _ = recast_snapshot(amount_paid=10_000_000)
    (position,) = snapshot.positions
    _resolve_from(monkeypatch, snapshot)
    monkeypatch.setattr(resolve, "reward_account_registered", lambda *_: False)
    with pytest.raises(ValueError, match="not registered"):
        RecastSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            recasts=[(position.out_ref, 10_000_000)],
            borrower_address=position.borrower_bond.address,
            valid_from=snapshot.valid_from,
            valid_to=snapshot.valid_to,
        )
    monkeypatch.setattr(resolve, "reward_account_registered", lambda *_: True)
    resolved = RecastSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        recasts=[(position.out_ref, 10_000_000)],
        borrower_address=position.borrower_bond.address,
        valid_from=snapshot.valid_from,
        valid_to=snapshot.valid_to,
    )
    assert resolved.positions[0].amount_paid == 10_000_000


def test_a_batch_keeps_open_loans_before_closing_ones() -> None:
    snapshot, _, slot = recast_batch_snapshot([10_000_000, 10_000_000, 10_000_000])
    # First loan pays half principal (continues), second and third pay full debt (close)
    valid_to_ms = slot_to_posix_ms(snapshot.valid_to)
    snapshot.positions[0].amount_paid = 5_000_000
    snapshot.positions[1].amount_paid = remaining_debt(
        snapshot.positions[1].loan_datum, valid_to_ms=valid_to_ms
    )
    snapshot.positions[2].amount_paid = remaining_debt(
        snapshot.positions[2].loan_datum, valid_to_ms=valid_to_ms
    )

    body = build(build_recast, snapshot, slot=slot).tx.transaction_body
    burned = {
        n.payload: q
        for p, names in body.mint.items()
        for n, q in names.items()
        if bytes(p).hex() == c.LOAN_POLICY
    }
    # Only second and third loans are burned
    assert burned == {
        snapshot.positions[1].loan_id: -1,
        snapshot.positions[2].loan_id: -1,
    }
    # First output is at the first loan's address (continuing)
    assert str(body.outputs[0].address) == snapshot.positions[0].loan.address
    datum = LoanDatum.from_cbor(body.outputs[0].datum.to_cbor())
    # Principal after paying 5M is the new principal computed by recast
    expected_principal = snapshot.new_principal(snapshot.positions[0])
    assert datum.principal_amount == expected_principal
    # Outputs 1-3 are at the asset-manager script
    for i in range(1, 4):
        assert (
            body.outputs[i].address.payment_part.payload.hex()
            == c.ASSET_MANAGER_SPEND_SKH
        )


def test_a_batch_closing_before_an_open_loan_raises() -> None:
    snapshot, _, slot = recast_batch_snapshot([10_000_000, 10_000_000, 10_000_000])
    valid_to_ms = slot_to_posix_ms(snapshot.valid_to)
    # First loan closes, second continues (wrong order!)
    snapshot.positions[0].amount_paid = remaining_debt(
        snapshot.positions[0].loan_datum, valid_to_ms=valid_to_ms
    )
    snapshot.positions[1].amount_paid = 5_000_000
    snapshot.positions[2].amount_paid = 10_000_000

    with pytest.raises(ValueError, match="sort before loans the recast closes"):
        build(build_recast, snapshot, slot=slot)


def test_paying_more_than_the_payoff_raises_naming_it() -> None:
    snapshot, _, slot = recast_snapshot(amount_paid=0)
    (position,) = snapshot.positions
    payoff = remaining_debt(
        position.loan_datum,
        valid_to_ms=slot_to_posix_ms(snapshot.valid_to),
    )
    position.amount_paid = payoff + 1
    with pytest.raises(ValueError, match=f"paid off by {payoff};"):
        build(build_recast, snapshot, slot=slot)


def test_an_amortized_payoff_is_its_remaining_principal() -> None:
    def amortized(datum: LoanDatum) -> LoanDatum:
        return replace(
            datum,
            repayment_mode=InterestOnRemainingPrincipal(max_possible_recasts=3),
            total_installments=6,
            installment_period=720,
            interest_rate=1200,
            repaid_installments=1,
        )

    snapshot, _, slot = recast_snapshot(amortized, amount_paid=0)
    (position,) = snapshot.positions
    datum = position.loan_datum
    payoff = finance.amortized_remaining_principal(
        principal=datum.principal_amount,
        interest_rate=datum.interest_rate,
        total_installments=datum.total_installments,
        repaid_installments=datum.repaid_installments,
    )
    position.amount_paid = payoff + 1
    with pytest.raises(ValueError, match=f"paid off by {payoff};"):
        build(build_recast, snapshot, slot=slot)
    position.amount_paid = payoff
    assert snapshot.new_principal(position) == 0


def test_from_backend_refuses_an_overpayment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _, _ = recast_snapshot(amount_paid=0)
    (position,) = snapshot.positions
    payoff = remaining_debt(
        position.loan_datum,
        valid_to_ms=slot_to_posix_ms(snapshot.valid_to),
    )
    _resolve_from(monkeypatch, snapshot)
    monkeypatch.setattr(resolve, "reward_account_registered", lambda *_: True)
    with pytest.raises(ValueError, match=f"paid off by {payoff};"):
        RecastSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            recasts=[(position.out_ref, payoff + 1)],
            borrower_address=position.borrower_bond.address,
            valid_from=snapshot.valid_from,
            valid_to=snapshot.valid_to,
        )


def test_a_continuing_recast_needs_the_loan_ada_to_cover_its_output() -> None:
    def recast_23_times(datum: LoanDatum) -> LoanDatum:
        return replace(
            datum,
            repayment_mode=PerpetualLoan(
                apy_increase_linear_coefficient=28,
                max_possible_recasts=100,
            ),
            done_recasts=23,
        )

    snapshot, _, slot = recast_snapshot(recast_23_times, amount_paid=10_000_000)
    (position,) = snapshot.positions
    # Exactly enough for the loan as it stands; the 24th recast adds a byte.
    position.loan = at_minimum_ada(position.loan)
    with pytest.raises(ValueError, match="requires the loan's value unchanged"):
        build(build_recast, snapshot, slot=slot)
    position.loan = replace(position.loan, lovelace=position.loan.lovelace + 4_310)
    loan = build(build_recast, snapshot, slot=slot).tx.transaction_body.outputs[0]
    assert LoanDatum.from_cbor(loan.datum.to_cbor()).done_recasts == 24
    assert loan.amount.coin == min_ada(loan)
