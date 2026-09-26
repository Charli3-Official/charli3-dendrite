"""What the V4 loan-action validators demand of a loan, computed exactly.

Each function mirrors a check of ``loan_repay_action.ak``, ``loan_recast_action.ak`` or
``loan_change_collateral_action.ak`` at a transaction's validity upper bound
``valid_to_ms`` (POSIX milliseconds), which is the time the validators read. Where the
contract would fail, these raise ``ValueError`` instead of returning a value the chain
rejects.
"""

from __future__ import annotations

from copy import copy
from fractions import Fraction

from charli3_dendrite.lending.fluidtokens import math as finance
from charli3_dendrite.lending.fluidtokens_v4.datums import Liquidation
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.state import decode_repayment_mode

# ``RepaymentMode`` constructor alternatives.
INTEREST_ON_REMAINING_PRINCIPAL = 0
PRINCIPAL_AND_INTEREST_ON_INSTALLMENTS = 1
PERPETUAL = 2

_MS_PER_HOUR = 3_600_000


def with_fields(datum: LoanDatum, **fields: int) -> LoanDatum:
    """A copy of ``datum`` with the integer ``fields`` replaced."""
    updated = copy(datum)
    for name, value in fields.items():
        setattr(updated, name, value)
    return updated


def _mode(datum: LoanDatum) -> tuple[int, tuple[int, ...]]:
    return decode_repayment_mode(datum.repayment_mode)


def is_perpetual(datum: LoanDatum) -> bool:
    """True for a perpetual loan."""
    return _mode(datum)[0] == PERPETUAL


def _perpetual_without_installments(datum: LoanDatum) -> bool:
    return is_perpetual(datum) and datum.installment_period == 0


def remaining_debt(datum: LoanDatum, *, valid_to_ms: int) -> int:
    """``finance.ak`` get_remaining_debt: everything owed at ``valid_to_ms``.

    A perpetual loan owes its principal plus the interest accrued since ``lend_date``
    (measured at any sign); an installment loan owes its remaining installments at the
    unpenalised installment amount.
    """
    alt, fields = _mode(datum)
    if alt == PERPETUAL:
        return finance.perpetual_debt(
            principal=datum.principal_amount,
            interest_rate=datum.interest_rate,
            apy_coef=fields[0],
            elapsed_ms=valid_to_ms - datum.lend_date,
            repaid_installments=datum.repaid_installments,
            installment_period=datum.installment_period,
            initial_grace_period=datum.initial_grace_period,
        )
    remaining = datum.total_installments - datum.repaid_installments
    return remaining * _installment(datum, is_late=False)


def is_late(datum: LoanDatum, *, valid_to_ms: int) -> bool:
    """True if the next installment is past its repayment window at ``valid_to_ms``."""
    return finance.is_repayment_late(
        is_perpetual=is_perpetual(datum),
        now_ms=valid_to_ms,
        lend_date_ms=datum.lend_date,
        initial_grace_period=datum.initial_grace_period,
        repaid_installments=datum.repaid_installments,
        installment_period=datum.installment_period,
        repayment_time_window=datum.repayment_time_window,
    )


def _installment(datum: LoanDatum, *, is_late: bool) -> int:
    """``finance.ak`` get_next_installment_amount, penalty included when late."""
    alt, _ = _mode(datum)
    penalty = datum.penalty_fee_for_late_repayment
    if alt == PERPETUAL:
        return finance.perpetual_installment_amount(
            principal=datum.principal_amount,
            interest_rate=datum.interest_rate,
            installment_period=datum.installment_period,
            initial_grace_period=datum.initial_grace_period,
            repaid_installments=datum.repaid_installments,
            is_late=is_late,
            penalty_fee=penalty,
        )
    if datum.total_installments <= 0:
        raise ValueError("an installment loan without installments cannot be repaid")
    if alt == INTEREST_ON_REMAINING_PRINCIPAL:
        if datum.interest_rate == 0:
            raise ValueError("a zero-rate amortized loan cannot be repaid on-chain")
        return finance.amortization_installment(
            principal=datum.principal_amount,
            interest_rate=datum.interest_rate,
            total_installments=datum.total_installments,
            is_late=is_late,
            penalty_fee=penalty,
        )
    return finance.installments_pi_amount(
        principal=datum.principal_amount,
        interest_rate=datum.interest_rate,
        total_installments=datum.total_installments,
        is_late=is_late,
        penalty_fee=penalty,
    )


def repayment_amount(datum: LoanDatum, *, valid_to_ms: int, is_final: bool) -> int:
    """The least a repay must pay the lender, in principal units.

    A perpetual loan repaid in full (``is_final``) or without installments pays its
    whole remaining debt; every other repay pays the next installment, with the late
    penalty once the installment is overdue.
    """
    if is_perpetual(datum) and (is_final or datum.installment_period == 0):
        return remaining_debt(datum, valid_to_ms=valid_to_ms)
    return _installment(datum, is_late=is_late(datum, valid_to_ms=valid_to_ms))


def repayment_closes_loan(datum: LoanDatum, *, is_final: bool) -> bool:
    """True if this repay closes the loan (burns its NFT, releases the collateral).

    A perpetual loan closes on a final repayment, and always when it has no
    installments; an installment loan closes on its last installment.
    """
    if is_perpetual(datum):
        return is_final or datum.installment_period == 0
    return datum.repaid_installments == datum.total_installments - 1


def repaid_loan_datum(datum: LoanDatum) -> LoanDatum:
    """The continuing loan's datum after one installment: one more repaid."""
    return with_fields(datum, repaid_installments=datum.repaid_installments + 1)


def recast_principal(datum: LoanDatum, *, amount_paid: int, valid_to_ms: int) -> int:
    """The loan's principal after a recast paying ``amount_paid`` at ``valid_to_ms``.

    Zero or less closes the loan. Raises ``ValueError`` when the contract would reject
    the recast: a non-positive payment, a principal-and-interest loan, no recasts left,
    installments still due, or (perpetual) a payment that does not cover the interest.
    """
    alt, fields = _mode(datum)
    if amount_paid <= 0:
        raise ValueError("a recast must pay a positive amount")
    if alt == PRINCIPAL_AND_INTEREST_ON_INSTALLMENTS:
        raise ValueError("a principal-and-interest loan cannot be recast")
    if datum.done_recasts >= fields[-1]:
        raise ValueError("the loan has no recasts left")
    if not _perpetual_without_installments(datum):
        _require_installments_paid(datum, valid_to_ms=valid_to_ms)
    if alt == PERPETUAL:
        debt = remaining_debt(datum, valid_to_ms=valid_to_ms)
        if amount_paid <= debt - datum.principal_amount:
            raise ValueError("a perpetual recast must pay more than the interest owed")
        return debt - amount_paid
    remaining = finance.amortized_remaining_principal(
        principal=datum.principal_amount,
        interest_rate=datum.interest_rate,
        total_installments=datum.total_installments,
        repaid_installments=datum.repaid_installments,
    )
    return remaining - amount_paid


def _require_installments_paid(datum: LoanDatum, *, valid_to_ms: int) -> None:
    """Raise unless every due installment, plus one, is already repaid."""
    if datum.installment_period <= 0:
        raise ValueError("a loan without installments can only be recast if perpetual")
    since_last = valid_to_ms - (
        datum.lend_date
        + (
            datum.initial_grace_period
            + datum.repaid_installments * datum.installment_period
        )
        * _MS_PER_HOUR
    )
    due = since_last // (datum.installment_period * _MS_PER_HOUR)
    if datum.repaid_installments < due + 1:
        raise ValueError("a recast needs every due installment, plus one, repaid")


def recast_loan_datum(datum: LoanDatum, *, new_principal: int) -> LoanDatum:
    """The continuing loan's datum after a recast to ``new_principal``.

    Both modes count the recast and carry the new principal. An amortized loan also
    restarts its installment count on the installments left and scales its rate to
    them (rounded up); a perpetual loan keeps every other term.
    """
    recast = with_fields(
        datum,
        done_recasts=datum.done_recasts + 1,
        principal_amount=new_principal,
    )
    if is_perpetual(datum):
        return recast
    left = datum.total_installments - datum.repaid_installments
    return with_fields(
        recast,
        repaid_installments=0,
        interest_rate=(datum.interest_rate * left + datum.total_installments - 1)
        // datum.total_installments,
        total_installments=left,
    )


def min_collateral(
    datum: LoanDatum,
    *,
    valid_to_ms: int,
    principal_price: Fraction,
    collateral_price: Fraction,
) -> int:
    """The least collateral a change-collateral may leave the loan with.

    Only a liquidation-mode loan can change its collateral; the loan must stay at or
    under its liquidation LTV at ``valid_to_ms``, with both prices in lovelace per
    smallest unit.
    """
    mode = datum.liquidation_mode
    if not isinstance(mode, Liquidation):
        raise ValueError("only a liquidation-mode loan can change its collateral")
    return finance.liquidation_min_collateral(
        debt=remaining_debt(datum, valid_to_ms=valid_to_ms),
        principal_price=principal_price,
        collateral_price=collateral_price,
        l_tv=mode.l_tv,
        l_tv_divider=mode.l_tv_divider,
    )
