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

Flow limits are pre-flighted in a fixed order: the market-level limits
(`check_market_limits`: ``p[1]``, ``p[14]``) before an action UTxO is chosen, then
the limits of the chosen action UTxO (`check_action_limits`: the caps, then its
liquidity), then health (`check_health`). Validity windows end by the market's
batch deadline ``s8 + p[3]`` (`valid_to_ms`).
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
from enum import Enum
from fractions import Fraction
from typing import Final

from charli3_dendrite.lending.liqwid import math as lqm
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import Rational
from charli3_dendrite.lending.liqwid.transactions.errors import CapExceededError
from charli3_dendrite.lending.liqwid.transactions.errors import CollateralCountError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MarketAwaitingBatchError
from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
from charli3_dendrite.lending.liqwid.transactions.errors import MissingFeedError
from charli3_dendrite.lending.liqwid.transactions.errors import RepayAmountError
from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    UnsupportedCollateralError,
)
from charli3_dendrite.lending.math import ceil_div
from charli3_dendrite.lending.math import floor_div

__all__ = [
    "DEFAULT_TTL_MS",
    "TTL_MARGIN_MS",
    "ActionDeltas",
    "Flow",
    "RepayResult",
    "Settlement",
    "apply_repay",
    "check_action_limits",
    "check_health",
    "check_limits",
    "check_market_limits",
    "close_deltas",
    "close_payment",
    "collateral_holdings",
    "default_valid_to_ms",
    "health_debt",
    "health_required",
    "increase",
    "open_loan",
    "rebase_for_collateral",
    "redeem_burn_for",
    "redeem_payout",
    "repay_from_settlement",
    "settle",
    "supply_mint",
    "ttl_cap_ms",
    "valid_to_ms",
]

DEFAULT_TTL_MS: Final[int] = 900_000  # a default window ends 15 minutes from now
TTL_MARGIN_MS: Final[int] = 1_000  # ... or a second before the batch deadline
_LOVELACE: Final[str] = "lovelace"
# Mainnet slots last one second and start on whole POSIX seconds, so two times fall
# in the same slot exactly when they fall in the same second.
_SLOT_MS: Final[int] = 1_000


class Flow(str, Enum):
    """The user flow a set of limits is checked for.

    ``REPAY`` is a partial repay and ``CLOSE`` the repay that pays a loan off;
    ``COLLATERAL`` is a collateral-only change. ``OPEN``, ``INCREASE``, ``REPAY``,
    ``CLOSE`` and ``COLLATERAL`` are the loan flows.
    """

    SUPPLY = "supply"
    REDEEM = "redeem"
    OPEN = "open"
    INCREASE = "increase"
    REPAY = "repay"
    CLOSE = "close"
    COLLATERAL = "collateral"


_MIN_VALUE_FLOWS: Final[frozenset[Flow]] = frozenset(
    {Flow.SUPPLY, Flow.REDEEM, Flow.OPEN, Flow.INCREASE, Flow.REPAY},
)
_LOAN_FLOWS: Final[frozenset[Flow]] = frozenset(
    {Flow.OPEN, Flow.INCREASE, Flow.REPAY, Flow.CLOSE, Flow.COLLATERAL},
)
_PAYOUT_FLOWS: Final[frozenset[Flow]] = frozenset(
    {Flow.REDEEM, Flow.OPEN, Flow.INCREASE},
)


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
        """``action`` with these deltas added field-wise; the nonce is kept.

        Args:
            action: The action UTxO's record before the flow.

        Returns:
            The record written back.
        """
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

        Returns:
            The signed change, positive when the action UTxO gains underlying.
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

    Args:
        amount: The underlying supplied, in base units.
        s9: The market's qToken rate (underlying per qToken).

    Returns:
        The qTokens minted.

    Raises:
        ValueError: On a negative amount or a non-positive rate.
    """
    return lqm.supply_mint(amount, _rate(s9))


def redeem_payout(qtokens: int, s9: Rational) -> int:
    """Most underlying a burn of ``qtokens`` may pay, ``floor(qtokens * s9)``.

    Args:
        qtokens: The qTokens burnt.
        s9: The market's qToken rate (underlying per qToken).

    Returns:
        The underlying paid out, in base units.

    Raises:
        ValueError: On a negative quantity or a non-positive rate.
    """
    rate = _rate(s9)
    _check_amount(qtokens, "qToken quantity")
    return floor_div(qtokens * rate.numerator, rate.denominator)


def redeem_burn_for(amount: int, s9: Rational) -> int:
    """Fewest qTokens whose payout is at least ``amount``, ``ceil(amount / s9)``.

    Args:
        amount: The underlying to be paid out, in base units.
        s9: The market's qToken rate (underlying per qToken).

    Returns:
        The qTokens to burn.

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

    Args:
        loan: The loan record before the repay.
        s5: The loan market's MarketState index.
        repay: The amount paid into the action UTxO.

    Returns:
        The loan record written back and the action deltas.

    Raises:
        ValueError: On a negative repay.
        RepayAmountError: When ``repay`` exceeds the settled debt.
    """
    return repay_from_settlement(loan, settle(loan, s5), repay)


def close_payment(loan: LoanDatum, s5: int) -> int:
    """The exact amount that closes ``loan``, ``p0 + max(iS, m0)``.

    Args:
        loan: The loan record.
        s5: The loan market's MarketState index.

    Returns:
        The payment in base units of the borrowed asset.
    """
    return lqm.payoff(loan, s5)


def close_deltas(loan: LoanDatum, s5: int) -> ActionDeltas:
    """Action deltas of a close: ``[0, 0, -p0, -iS, max(0, m0 - iS)]``.

    The close is the only flow that tops up the minimum interest; the action's
    underlying grows by exactly `close_payment`.

    Args:
        loan: The loan record.
        s5: The loan market's MarketState index.

    Returns:
        The action deltas of the close.
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

    Args:
        loan: The loan record.
        s5: The loan market's MarketState index.

    Returns:
        The loan record written back (``loan`` itself when unchanged).
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


# --- flow limits ------------------------------------------------------------------


def check_market_limits(
    flow: Flow,
    *,
    params: MarketParamsDatum,
    amount: int,
    output_collateral_count: int,
) -> None:
    """Market-level limits, checked before an action UTxO is chosen.

    First ``p[1]``: the action amount (supply ``A``, redeem payout ``S``, open or
    borrow-more ``A``, partial repay ``R``) must be at least the market minimum; a
    close and a collateral-only change are exempt. Then ``p[14]`` on every loan
    flow: the output loan may hold at most ``p[14]`` distinct collateral assets, so
    ``p[14] = 0`` refuses any collateral.

    Args:
        flow: The flow being built.
        params: The market's MarketParams.
        amount: The flow's action amount in underlying base units (ignored by a
            close and a collateral change).
        output_collateral_count: Distinct collateral assets in the output loan (0
            when no loan output remains).

    Raises:
        MinValueError: When ``amount`` is below ``p[1]``.
        CollateralCountError: When the output loan holds more than ``p[14]``
            collateral assets.
    """
    if flow in _MIN_VALUE_FLOWS and amount < params.min_value:
        raise MinValueError(amount, params.min_value)
    if flow in _LOAN_FLOWS and output_collateral_count > params.max_collaterals:
        raise CollateralCountError(output_collateral_count, params.max_collaterals)


def _action_count(params: MarketParamsDatum) -> int:
    count = params.action_count
    if count <= 0:
        raise ValueError(f"liqwid market params: non-positive action count {count}")
    return count


def check_action_limits(
    flow: Flow,
    *,
    params: MarketParamsDatum,
    state: MarketStateDatum,
    action_datum: ActionDatum,
    deltas: ActionDeltas,
    action_underlying: int,
    underlying: str,
) -> None:
    """Limits of one chosen action UTxO after this transaction's ``deltas``.

    With ``p7`` action UTxOs, each is held to its share of the market:

    - ``p[33]`` (supply cap), only when ``deltas`` add supply:
      ``floor(s0/p7) + floor(s3/p7) + dS_acc <= floor(p[33]/p7)``, where ``dS_acc``
      is the action UTxO's accumulated supply delta after this transaction.
    - ``p[32]`` (borrow cap), only when ``deltas`` add principal:
      ``(floor(s3/p7) + dP_acc) / (floor(s0/p7) + floor(s3/p7)) <= p[32]``, with
      ``dP_acc`` the accumulated principal delta after this transaction.
    - Liquidity, on a redeem, open or borrow more: the action UTxO pays out
      ``-deltas.underlying_delta()`` from its underlying, less the MarketState's
      recorded minimum ``s10`` on the Ada market.

    A market already over a cap still redeems and repays: the caps bind only the
    flows that add to them.

    Args:
        flow: The flow being built.
        params: The market's MarketParams.
        state: The market's current MarketState.
        action_datum: The chosen action UTxO's record before this transaction.
        deltas: This transaction's deltas on that action UTxO.
        action_underlying: The action UTxO's underlying holding, in base units
            (lovelace on the Ada market).
        underlying: The market's underlying unit (``"lovelace"`` for Ada).

    Raises:
        CapExceededError: When the supply or borrow cap would be passed.
        InsufficientLiquidityError: When the action UTxO cannot pay out.
    """
    p7 = _action_count(params)
    cash, principal = state.cash // p7, state.principal // p7
    if deltas.supply > 0 and params.supply_cap is not None:
        supplied = action_datum.supply_delta + deltas.supply
        if cash + principal + supplied > params.supply_cap // p7:
            raise CapExceededError("supply")
    if deltas.principal > 0 and params.borrow_cap is not None:
        num, den = params.borrow_cap
        lent = action_datum.principal_delta + deltas.principal
        if (principal + lent) * den > num * (cash + principal):
            raise CapExceededError("borrow")
    if flow in _PAYOUT_FLOWS:
        needed = -deltas.underlying_delta()
        floor = state.min_ada if underlying == _LOVELACE else 0
        available = max(0, action_underlying - floor)
        if needed > available:
            raise InsufficientLiquidityError(needed, available)


def check_limits(
    flow: Flow,
    *,
    params: MarketParamsDatum,
    state: MarketStateDatum,
    action_datum: ActionDatum,
    deltas: ActionDeltas,
    amount: int,
    output_collateral_count: int,
    action_underlying: int,
    underlying: str,
) -> None:
    """`check_market_limits` then `check_action_limits`, for a pinned action UTxO.

    Args:
        flow: The flow being built.
        params: The market's MarketParams.
        state: The market's current MarketState.
        action_datum: The chosen action UTxO's record before this transaction.
        deltas: This transaction's deltas on that action UTxO.
        amount: The flow's action amount in underlying base units (ignored by a
            close and a collateral change).
        output_collateral_count: Distinct collateral assets in the output loan (0
            when no loan output remains).
        action_underlying: The action UTxO's underlying holding, in base units
            (lovelace on the Ada market).
        underlying: The market's underlying unit (``"lovelace"`` for Ada).

    Raises:
        MinValueError: When ``amount`` is below ``p[1]``.
        CollateralCountError: When the output loan holds too many collaterals.
        CapExceededError: When the supply or borrow cap would be passed.
        InsufficientLiquidityError: When the action UTxO cannot pay out.
    """
    check_market_limits(
        flow,
        params=params,
        amount=amount,
        output_collateral_count=output_collateral_count,
    )
    check_action_limits(
        flow,
        params=params,
        state=state,
        action_datum=action_datum,
        deltas=deltas,
        action_underlying=action_underlying,
        underlying=underlying,
    )


# --- health -----------------------------------------------------------------------


def collateral_holdings(
    collateral: Mapping[str, int],
    *,
    cparams: Mapping[str, CollateralParamsDatum],
    rates: Mapping[str, Rational],
    prices: Mapping[str, Rational],
) -> list[lqm.WeightedHolding]:
    """A loan's collateral valued for the health check, in ``collateral`` order.

    Each unit is priced by its exact CollateralParams record of the borrow market:
    ``amount * s9 * price * w``, with ``s9`` the collateral market's qToken rate,
    ``price`` the price of the record's underlying and ``w`` the record's weight
    (no liquidation threshold applied). Zero quantities are skipped.

    Args:
        collateral: Collateral unit -> quantity held by the loan.
        cparams: Collateral unit -> its CollateralParams record.
        rates: Collateral unit -> its market's qToken rate ``s9``.
        prices: Underlying unit -> price per base unit, in the numeraire the
            borrowed asset's price is given in.

    Returns:
        One holding per held unit.

    Raises:
        UnsupportedCollateralError: When a unit has no CollateralParams record,
            only the policy-wide one, or a record naming no collateral market.
        MissingFeedError: When a collateral's underlying has no price.
        ValueError: On a negative quantity or a unit without a rate.
    """
    holdings = []
    for unit, amount in collateral.items():
        if amount < 0:
            raise ValueError(f"negative collateral quantity {amount} of {unit}")
        if amount == 0:
            continue
        record = cparams.get(unit)
        if record is None or record.policy_wide or not record.underlying:
            raise UnsupportedCollateralError(unit)
        rate = rates.get(unit)
        if rate is None:
            raise ValueError(f"no qToken rate for collateral {unit}")
        price = prices.get(record.underlying)
        if price is None:
            raise MissingFeedError(record.underlying)
        holdings.append(
            lqm.WeightedHolding(
                amount=amount,
                rate=Fraction(*rate),
                price=Fraction(*price),
                weight=Fraction(*record.weight),
            ),
        )
    return holdings


def _held_units(holding: Mapping[str, int]) -> dict[str, int]:
    return {unit: amount for unit, amount in holding.items() if amount != 0}


def health_required(
    borrow: int,
    repay: int,
    collateral_in: Mapping[str, int],
    collateral_out: Mapping[str, int],
) -> bool:
    """Whether the loan validator checks health for this change.

    It does when principal is lent (``borrow > 0``), when any collateral quantity
    decreases (a missing unit counts as 0), and on a collateral-only add
    (``borrow == repay == 0`` with the collateral changed). A repay whose
    collateral is unchanged or only increased is not checked.

    Args:
        borrow: New principal lent by this transaction.
        repay: Amount repaid by this transaction.
        collateral_in: Collateral unit -> quantity of the input loan.
        collateral_out: Collateral unit -> quantity of the output loan.

    Returns:
        True when the health rule applies.
    """
    if borrow > 0:
        return True
    if any(collateral_out.get(unit, 0) < q for unit, q in collateral_in.items()):
        return True
    changed = _held_units(collateral_in) != _held_units(collateral_out)
    return borrow == 0 and repay == 0 and changed


def check_health(
    holdings: Sequence[lqm.WeightedHolding],
    debt: int,
    borrow_price: Fraction,
    max_ltv: Fraction,
) -> None:
    """The loan validator's health rule, exact and inclusive.

    ``max_ltv * sum(weighted value) >= debt * borrow_price``; a loan without debt
    passes.

    Args:
        holdings: The output loan's collateral (`collateral_holdings`).
        debt: `health_debt` of the change, in borrowed base units.
        borrow_price: The borrowed asset's price per base unit, in the holdings'
            numeraire.
        max_ltv: The borrow market's maximum LTV ``p[12]``.

    Raises:
        ValueError: When ``borrow_price`` or ``max_ltv`` is not positive.
        UnhealthyLoanError: When the debt value exceeds the allowed collateral
            value.
    """
    if borrow_price <= 0 or max_ltv <= 0:
        msg = f"non-positive borrow price {borrow_price} or max LTV {max_ltv}"
        raise ValueError(msg)
    if debt <= 0:
        return
    required = debt * borrow_price
    weighted = sum((holding.weighted_value for holding in holdings), Fraction(0))
    available = max_ltv * weighted
    if available < required:
        raise UnhealthyLoanError(required=required, available=available)


# --- validity ---------------------------------------------------------------------


def ttl_cap_ms(state: MarketStateDatum, params: MarketParamsDatum) -> int:
    """The market's batch deadline ``s8 + p[3]``, the latest valid upper bound.

    Args:
        state: The market's current MarketState (``s8``, its last batch time).
        params: The market's MarketParams (``p[3]``, the longest batch interval).

    Returns:
        The deadline in POSIX milliseconds.
    """
    return state.last_batch_ms + params.max_batch_time_ms


def default_valid_to_ms(
    now_ms: int,
    state: MarketStateDatum,
    params: MarketParamsDatum,
) -> int:
    """Default upper bound: 15 minutes from now, or a second before the deadline.

    ``min(now + 900_000, s8 + p[3] - 1_000)`` in POSIX milliseconds.

    Args:
        now_ms: The current time.
        state: The market's current MarketState.
        params: The market's MarketParams.

    Returns:
        The default upper bound, in POSIX milliseconds.
    """
    return min(now_ms + DEFAULT_TTL_MS, ttl_cap_ms(state, params) - TTL_MARGIN_MS)


def valid_to_ms(
    now_ms: int,
    requested_ms: int | None,
    *,
    state: MarketStateDatum,
    params: MarketParamsDatum,
    market: str,
) -> int:
    """The upper validity bound of an action-spending flow, in POSIX milliseconds.

    The requested bound (or `default_valid_to_ms` when ``None``) capped at the
    deadline ``s8 + p[3]`` (inclusive). The bound must fall in a slot after the
    current one; when it cannot (now is at or within a second of the deadline,
    or the requested bound is not in the future), the market has to batch first.

    Args:
        now_ms: The current time.
        requested_ms: The caller's upper bound, or ``None`` for the default.
        state: The market's current MarketState.
        params: The market's MarketParams.
        market: The market's name, for the error.

    Returns:
        The upper bound.

    Raises:
        MarketAwaitingBatchError: When no bound after the current slot reaches
            the deadline.
    """
    cap = ttl_cap_ms(state, params)
    wanted = (
        default_valid_to_ms(now_ms, state, params)
        if requested_ms is None
        else requested_ms
    )
    result = min(wanted, cap)
    if result // _SLOT_MS <= now_ms // _SLOT_MS:
        raise MarketAwaitingBatchError(market, cap)
    return result
