"""What the V4 loan-action validators demand of a loan, on captured mainnet loans."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from charli3_dendrite.lending.fluidtokens.datums import InterestOnRemainingPrincipal
from charli3_dendrite.lending.fluidtokens.datums import (
    PrincipalAndInterestOnInstallments,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import (
    NoLiquidationFullCollateralClaim,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions import loan_terms as terms
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.fluidtokens_v4.transactions.replay import fixture

_HOUR = 3_600_000


def _loans(name: str) -> tuple[list[LoanDatum], int]:
    """The captured loans (in input order) and the capture's upper bound in ms."""
    fix = fixture(name)
    loans = [
        LoanDatum.from_cbor(u.datum)
        for u in sorted(
            (utxo_from_dict(d) for d in fix["inputs"]),
            key=lambda u: (bytes.fromhex(u.out_ref[0]), u.out_ref[1]),
        )
        if u.holds_policy(c.LOAN_POLICY) and u.datum
    ]
    return loans, slot_to_posix_ms(fix["invalid_hereafter"])


def _amortized(datum: LoanDatum, **changes: int) -> LoanDatum:
    """``datum`` as a 6-installment amortized loan at 12%, 30-day installments."""
    amortized = {
        "repayment_mode": InterestOnRemainingPrincipal(max_possible_recasts=3),
        "total_installments": 6,
        "installment_period": 720,
        "interest_rate": 1200,
    }
    return replace(datum, **(amortized | changes))


def test_full_repayment_amounts_match_the_contract() -> None:
    (loan,), valid_to = _loans("repay_single")
    assert terms.repayment_amount(loan, valid_to_ms=valid_to, is_final=True) == (
        20_000_004
    )
    loans, valid_to = _loans("repay_multi")
    # Repaid before their lend date: the contract's debt is below the principal.
    assert [
        terms.repayment_amount(d, valid_to_ms=valid_to, is_final=True) for d in loans
    ] == [59_999_957, 59_999_957, 379_999_723]


def test_perpetual_loan_without_installments_always_closes() -> None:
    (loan,), _ = _loans("repay_single")
    assert terms.repayment_closes_loan(loan, is_final=False)
    assert terms.repayment_closes_loan(loan, is_final=True)


def test_perpetual_installment_repay_continues_the_loan() -> None:
    (loan,), valid_to = _loans("repay_single")
    loan = replace(loan, installment_period=720)
    assert not terms.repayment_closes_loan(loan, is_final=False)
    # The first installment covers the grace period plus one period of interest.
    assert terms.repayment_amount(loan, valid_to_ms=valid_to, is_final=False) == (
        74_302
    )
    assert terms.repaid_loan_datum(loan).repaid_installments == 1


def test_installment_loan_closes_on_its_last_installment() -> None:
    (loan,), valid_to = _loans("repay_single")
    first = _amortized(loan)
    last = _amortized(loan, repaid_installments=5)
    assert not terms.repayment_closes_loan(first, is_final=True)
    assert terms.repayment_closes_loan(last, is_final=False)
    amount = terms.repayment_amount(first, valid_to_ms=valid_to, is_final=False)
    assert amount == 3_570_517
    assert terms.repayment_amount(last, valid_to_ms=valid_to, is_final=False) == amount


def test_a_late_installment_pays_the_penalty() -> None:
    (loan,), _ = _loans("repay_single")
    loan = _amortized(loan, penalty_fee_for_late_repayment=1_000)
    due = loan.lend_date + (loan.initial_grace_period + 720) * _HOUR
    on_time = terms.repayment_amount(loan, valid_to_ms=due, is_final=False)
    late = terms.repayment_amount(loan, valid_to_ms=due + 1, is_final=False)
    assert late == on_time + 1_000


def test_installment_loans_the_contract_cannot_repay_raise() -> None:
    (loan,), valid_to = _loans("repay_single")
    with pytest.raises(ValueError, match="zero-rate"):
        terms.repayment_amount(
            _amortized(loan, interest_rate=0),
            valid_to_ms=valid_to,
            is_final=False,
        )
    with pytest.raises(ValueError, match="without installments"):
        terms.repayment_amount(
            _amortized(loan, total_installments=0),
            valid_to_ms=valid_to,
            is_final=False,
        )


def test_min_collateral_matches_the_contract() -> None:
    (loan,), valid_to = _loans("change_collateral_single")
    assert (
        terms.min_collateral(
            loan,
            valid_to_ms=valid_to,
            principal_price=Fraction(1),
            collateral_price=Fraction(225419077, 100000000),
        )
        == 1_219_978_267
    )


def test_only_liquidation_loans_change_collateral() -> None:
    (loan,), valid_to = _loans("change_collateral_single")
    with pytest.raises(ValueError, match="liquidation-mode"):
        terms.min_collateral(
            replace(loan, liquidation_mode=NoLiquidationFullCollateralClaim()),
            valid_to_ms=valid_to,
            principal_price=Fraction(1),
            collateral_price=Fraction(1),
        )


def test_perpetual_recast() -> None:
    (loan,), valid_to = _loans("repay_single")
    debt = terms.remaining_debt(loan, valid_to_ms=valid_to)
    assert (
        terms.recast_principal(loan, amount_paid=10_000_000, valid_to_ms=valid_to)
        == debt - 10_000_000
    )
    recast = terms.recast_loan_datum(loan, new_principal=10_000_004)
    assert (recast.done_recasts, recast.principal_amount) == (1, 10_000_004)
    assert replace(recast, done_recasts=0, principal_amount=20_000_000) == loan
    with pytest.raises(ValueError, match="more than the interest"):
        terms.recast_principal(
            loan,
            amount_paid=debt - loan.principal_amount,
            valid_to_ms=valid_to,
        )


def test_amortized_recast_restarts_the_installments() -> None:
    (loan,), valid_to = _loans("repay_single")
    loan = _amortized(loan, repaid_installments=1)
    assert (
        terms.recast_principal(loan, amount_paid=5_000_000, valid_to_ms=valid_to)
        == 11_829_483
    )
    recast = terms.recast_loan_datum(loan, new_principal=11_829_483)
    assert (
        recast.done_recasts,
        recast.repaid_installments,
        recast.total_installments,
        recast.interest_rate,
        recast.lend_date,
    ) == (1, 0, 5, 1000, loan.lend_date)


def test_recasts_the_contract_rejects_raise() -> None:
    (loan,), valid_to = _loans("repay_single")
    with pytest.raises(ValueError, match="positive amount"):
        terms.recast_principal(loan, amount_paid=0, valid_to_ms=valid_to)
    with pytest.raises(ValueError, match="no recasts left"):
        terms.recast_principal(
            replace(loan, done_recasts=5),
            amount_paid=1,
            valid_to_ms=valid_to,
        )
    with pytest.raises(ValueError, match="principal-and-interest"):
        terms.recast_principal(
            replace(loan, repayment_mode=PrincipalAndInterestOnInstallments()),
            amount_paid=1,
            valid_to_ms=valid_to,
        )
    overdue = _amortized(loan, lend_date=loan.lend_date - 800 * _HOUR)
    with pytest.raises(ValueError, match="every due installment"):
        terms.recast_principal(overdue, amount_paid=1, valid_to_ms=valid_to)
