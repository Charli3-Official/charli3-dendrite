"""Exact arithmetic of the Liqwid V2 user flows.

Pure functions over decoded Liqwid records (``liqwid/datums.py``): integers in base
units and exact rationals, rounded the way the validators round. Nothing here reads
the chain, builds a transaction or prices an asset.

Symbols follow the records: a loan is ``(owner, p0, i0, m0, x0)`` (principal,
interest, minimum interest, index), ``s5`` is the loan market's MarketState
index and ``s9`` a MarketState's qToken rate (underlying per qToken, an
unreduced rational). ``owed = p0 + i0``.

Settlement (applied before every loan modification) brings a loan to ``s5``:
``Ds = max(ceil(owed * s5 / x0), owed)`` is the settled debt, ``iS = Ds - p0`` the
settled interest and ``x1 = floor(x0 * Ds / owed)`` the index written back. It is
not a time projection: the written index is the old one rebased to the rounded-up
debt.

An action UTxO records a flow as signed deltas ``[supply, qTokens, principal,
interest, min_interest_paid]`` (`ActionDeltas`), and its value changes by
``supply - principal - interest + min_interest_paid`` of the underlying.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from fractions import Fraction

from charli3_dendrite.lending.liqwid import math as lqm
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import Rational
from charli3_dendrite.lending.liqwid.transactions.errors import RepayAmountError
from charli3_dendrite.lending.math import ceil_div
from charli3_dendrite.lending.math import floor_div

__all__ = [
    "ActionDeltas",
    "RepayResult",
    "Settlement",
    "apply_repay",
    "close_deltas",
    "close_payment",
    "health_debt",
    "increase",
    "open_loan",
    "rebase_for_collateral",
    "redeem_burn_for",
    "redeem_payout",
    "repay_from_settlement",
    "settle",
    "supply_mint",
]


@dataclass(frozen=True)
class Settlement:
    """A loan brought to its market's index.

    Attributes:
        debt: The settled debt ``Ds`` in base units of the borrowed asset.
        interest: The settled interest ``iS = Ds - p0``.
        index: The loan index written back, ``x1``.
        clamped: True when the indexed debt rounded below ``owed`` and the debt
            was held at ``owed`` (a loan rebased after the market's last batch).
    """

    debt: int
    interest: int
    index: int
    clamped: bool


@dataclass(frozen=True)
class ActionDeltas:
    """Signed changes one flow makes to an action UTxO's record.

    Attributes:
        supply: Underlying supplied (negative on a redeem).
        qtoken: QTokens minted (negative when burnt).
        principal: Principal lent out (negative when repaid).
        interest: Settled interest added (negative when paid).
        min_interest_paid: Minimum interest topped up by a close.
    """

    supply: int = 0
    qtoken: int = 0
    principal: int = 0
    interest: int = 0
    min_interest_paid: int = 0

    def apply_to(self, action: ActionDatum) -> ActionDatum:
        """``action`` with these deltas added field-wise; the nonce is kept."""
        return replace(
            action,
            supply_delta=action.supply_delta + self.supply,
            qtoken_delta=action.qtoken_delta + self.qtoken,
            principal_delta=action.principal_delta + self.principal,
            interest_delta=action.interest_delta + self.interest,
            min_interest_paid=action.min_interest_paid + self.min_interest_paid,
        )

    def underlying_delta(self) -> int:
        """Change of the action UTxO's underlying, in base units.

        ``supply - principal - interest + min_interest_paid``: supplies and
        repayments flow in, redeems and borrows flow out.
        """
        return self.supply - self.principal - self.interest + self.min_interest_paid


@dataclass(frozen=True)
class RepayResult:
    """A partial repay: the loan record written back and the action deltas."""

    loan: LoanDatum
    deltas: ActionDeltas


def _rate(s9: Rational) -> Fraction:
    num, den = s9
    if num <= 0 or den <= 0:
        raise ValueError(f"non-positive qToken rate {num}/{den}")
    return Fraction(num, den)


def _check_amount(amount: int, what: str) -> None:
    if amount < 0:
        raise ValueError(f"negative {what} {amount}")


def settle(loan: LoanDatum, s5: int) -> Settlement:
    """Bring ``loan`` to the market index ``s5`` (the settlement above).

    A loan that owes nothing, or carries no index, keeps its index and settles at
    what it owes.

    Args:
        loan: The loan record.
        s5: The loan market's MarketState index.

    Returns:
        The settled debt, interest and index.
    """
    owed = loan.principal + loan.interest
    debt = lqm.liquidation_debt(loan, s5)
    if owed == 0 or loan.index <= 0:
        return Settlement(debt, debt - loan.principal, loan.index, clamped=False)
    return Settlement(
        debt=debt,
        interest=debt - loan.principal,
        index=floor_div(loan.index * debt, owed),
        clamped=ceil_div(owed * s5, loan.index) < owed,
    )


def supply_mint(amount: int, s9: Rational) -> int:
    """QTokens minted for supplying ``amount``, ``floor(amount * den / num)``.

    Raises:
        ValueError: On a negative amount or a non-positive rate.
    """
    return lqm.supply_mint(amount, _rate(s9))


def redeem_payout(qtokens: int, s9: Rational) -> int:
    """Most underlying a burn of ``qtokens`` may pay, ``floor(qtokens * s9)``.

    Raises:
        ValueError: On a negative quantity or a non-positive rate.
    """
    rate = _rate(s9)
    _check_amount(qtokens, "qToken quantity")
    return floor_div(qtokens * rate.numerator, rate.denominator)


def redeem_burn_for(amount: int, s9: Rational) -> int:
    """Fewest qTokens whose payout is at least ``amount``, ``ceil(amount / s9)``.

    Raises:
        ValueError: On a negative amount or a non-positive rate.
    """
    rate = _rate(s9)
    _check_amount(amount, "redeem amount")
    return ceil_div(amount * rate.denominator, rate.numerator)


def _check_borrow(amount: int) -> None:
    if amount <= 0:
        raise ValueError(f"non-positive borrow amount {amount}")


def _min_interest(amount: int, params: MarketParamsDatum) -> int:
    return lqm.min_interest_at_borrow(amount, Fraction(*params.min_interest_rate))


def open_loan(owner: str, amount: int, s5: int, params: MarketParamsDatum) -> LoanDatum:
    """The record of a new loan of ``amount``: ``(owner, A, 0, ceil(A * p15), s5)``.

    Args:
        owner: The borrower's key hash (hex).
        amount: The principal lent, in base units of the borrowed asset.
        s5: The borrow market's MarketState index.
        params: The borrow market's MarketParams (``p[15]``, the minimum interest
            rate).

    Returns:
        The loan record.

    Raises:
        ValueError: On a non-positive amount.
    """
    _check_borrow(amount)
    return LoanDatum(owner, amount, 0, _min_interest(amount, params), s5)


def increase(
    loan: LoanDatum,
    s5: int,
    amount: int,
    params: MarketParamsDatum,
) -> tuple[LoanDatum, ActionDeltas]:
    """Borrow ``amount`` more on a loan.

    The loan is settled, then ``p1 = p0 + A``, ``i1 = iS``, ``m1 = m0 + ceil(A *
    p15)`` at the settled index. The action records ``+A`` of principal; the
    settled interest is not added to it.

    Args:
        loan: The loan record.
        s5: The borrow market's MarketState index.
        amount: The additional principal.
        params: The borrow market's MarketParams.

    Returns:
        The loan record written back and the action deltas.

    Raises:
        ValueError: On a non-positive amount.
    """
    _check_borrow(amount)
    settlement = settle(loan, s5)
    new_loan = LoanDatum(
        loan.owner,
        loan.principal + amount,
        settlement.interest,
        loan.min_interest + _min_interest(amount, params),
        settlement.index,
    )
    return new_loan, ActionDeltas(principal=amount)


def repay_from_settlement(
    loan: LoanDatum,
    settlement: Settlement,
    repay: int,
) -> RepayResult:
    """Repay ``repay`` on a loan already settled to ``settlement``.

    Interest is paid first: when ``R <= iS`` the principal is kept and ``i1 = iS
    - R``; otherwise ``i1 = 0`` and ``p1 = p0 - (R - iS)``. The minimum interest
    becomes ``max(0, m0 - iS)`` whatever ``R`` is, and the index is the
    settlement's. The action records the principal and interest paid.

    ``R`` is bounded by the settled debt ``Ds``. Repaying ``Ds`` leaves a loan that
    owes only ``max(0, m0 - iS)`` of minimum interest; the payoff ``p0 + max(iS,
    m0)`` closes the loan instead (`close_deltas`). No flow limits are checked.

    Args:
        loan: The loan record before the repay.
        settlement: ``settle(loan, s5)``, or a settlement the caller holds.
        repay: The amount paid into the action UTxO.

    Returns:
        The loan record written back and the action deltas.

    Raises:
        ValueError: On a negative repay.
        RepayAmountError: When ``repay`` exceeds the settled debt.
    """
    _check_amount(repay, "repay")
    if repay > settlement.debt:
        payoff = loan.principal + max(settlement.interest, loan.min_interest)
        raise RepayAmountError(repay, settlement.debt, payoff)
    settled = settlement.interest
    if repay <= settled:
        principal, interest = loan.principal, settled - repay
    else:
        principal, interest = loan.principal - (repay - settled), 0
    new_loan = LoanDatum(
        loan.owner,
        principal,
        interest,
        max(0, loan.min_interest - settled),
        settlement.index,
    )
    deltas = ActionDeltas(
        principal=principal - loan.principal,
        interest=interest - settled,
    )
    return RepayResult(new_loan, deltas)


def apply_repay(loan: LoanDatum, s5: int, repay: int) -> RepayResult:
    """Settle ``loan`` to ``s5`` and repay ``repay`` (`repay_from_settlement`).

    Raises:
        ValueError: On a negative repay.
        RepayAmountError: When ``repay`` exceeds the settled debt.
    """
    return repay_from_settlement(loan, settle(loan, s5), repay)


def close_payment(loan: LoanDatum, s5: int) -> int:
    """The exact amount that closes ``loan``, ``p0 + max(iS, m0)``."""
    return lqm.payoff(loan, s5)


def close_deltas(loan: LoanDatum, s5: int) -> ActionDeltas:
    """Action deltas of a close: ``[0, 0, -p0, -iS, max(0, m0 - iS)]``.

    The close is the only flow that tops up the minimum interest; the action's
    underlying grows by exactly `close_payment`.
    """
    settled = settle(loan, s5).interest
    return ActionDeltas(
        principal=-loan.principal,
        interest=-settled,
        min_interest_paid=max(0, loan.min_interest - settled),
    )


def rebase_for_collateral(loan: LoanDatum, s5: int) -> LoanDatum:
    """The loan record a collateral-only change writes back.

    Unchanged when the loan index is at or above ``s5``; otherwise settled:
    ``(p0, iS, m0, x1)``.
    """
    if loan.index >= s5:
        return loan
    settlement = settle(loan, s5)
    return LoanDatum(
        loan.owner,
        loan.principal,
        settlement.interest,
        loan.min_interest,
        settlement.index,
    )


def health_debt(p0: int, i_s: int, m0: int, borrow: int, repay: int) -> int:
    """Debt the health check measures, ``p0 + max(iS, m0) + borrow - repay``.

    Args:
        p0: Principal before the transaction.
        i_s: Settled interest ``iS``.
        m0: Minimum interest before the transaction.
        borrow: New principal lent by this transaction (0 when none).
        repay: Amount repaid by this transaction (0 when none).

    Returns:
        The debt in base units of the borrowed asset.
    """
    return p0 + max(i_s, m0) + borrow - repay
