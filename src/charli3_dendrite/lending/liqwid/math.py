"""Liqwid V2 interest, debt, rate and health arithmetic.

Pure functions over decoded Liqwid V2 records (``datums.py``). Amounts are integers
in base units and ratios are exact ``Fraction``s, rounded the way the protocol
rounds: debt up, minted, repaid and seized amounts down. Only the yearly rates are
``Decimal``: a borrow APY compounds the per-period rate 15,768 times a year, a power
of a rational too large to carry exactly, so it is computed to 60 significant
digits.

Symbols follow the record layouts: ``s0`` cash, ``s2`` qTokens in circulation,
``s3`` principal, ``s4`` interest, ``s5`` market index, ``s6`` borrow rate per
period and ``s9`` qToken rate of a MarketState; ``p[2]`` income ratio, ``p[10]``
interest period, ``p[11]`` rate model, ``p[15]`` minimum interest rate, ``p[16]``
close factor and ``p[18]`` liquidation threshold of a MarketParams record.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from decimal import localcontext
from fractions import Fraction
from typing import Final

from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import Rational
from charli3_dendrite.lending.math import borrow_headroom
from charli3_dendrite.lending.math import ceil_div
from charli3_dendrite.lending.math import floor_div
from charli3_dendrite.lending.rates import HOURS_PER_YEAR
from charli3_dendrite.lending.rates import MS_PER_HOUR

__all__ = [
    "APY_PRECISION",
    "INTEREST_PERIOD_MS",
    "YEAR_MS",
    "WeightedHolding",
    "apy_fraction",
    "borrow_apy",
    "borrow_headroom",
    "borrow_rate",
    "health",
    "liquidation_debt",
    "liquidation_repay",
    "liquidation_weight",
    "ltv",
    "min_interest_at_borrow",
    "payoff",
    "qtoken_rate",
    "seized_qtokens",
    "seized_value",
    "supply_apy",
    "supply_mint",
    "utilization",
]

INTEREST_PERIOD_MS: Final[int] = 2_000_000  # p[10] on every market
YEAR_MS: Final[int] = HOURS_PER_YEAR * MS_PER_HOUR  # 365 days
APY_PRECISION: Final[int] = 60  # significant digits of a yearly rate
_RATE_MODEL_TERMS: Final[int] = 4  # p[11] = [base, multiplier, jump, kink]
_INCOME_RATIO_TERMS: Final[int] = 2  # p[2] starts [total, supplier share, ...]


def _fraction(value: Rational) -> Fraction:
    return Fraction(value[0], value[1])


def _decimal(value: Fraction) -> Decimal:
    """``value`` as a Decimal, rounded in the current decimal context."""
    return Decimal(value.numerator) / Decimal(value.denominator)


def _supplier_share(income_ratio: Sequence[int]) -> Fraction:
    """The suppliers' share of borrow interest, ``p[2][1] / p[2][0]``."""
    if len(income_ratio) < _INCOME_RATIO_TERMS or income_ratio[0] <= 0:
        raise ValueError(f"malformed income ratio {tuple(income_ratio)}")
    return Fraction(income_ratio[1], income_ratio[0])


def utilization(state: MarketStateDatum) -> Fraction:
    """Borrowed share of a market's funds, ``(s3 + s4) / (s0 + s3 + s4)``.

    Args:
        state: The market's MarketState record.

    Returns:
        The utilization; 0 for a market that holds nothing.
    """
    borrowed = state.principal + state.interest
    total = state.cash + borrowed
    if total <= 0:
        return Fraction(0)
    return Fraction(borrowed, total)


def borrow_rate(model: Sequence[Rational], util: Fraction) -> Fraction:
    """Borrow rate per interest period from the kinked model ``p[11]``.

    The rate is ``base + multiplier * util``, plus ``jump * (util - kink)`` once the
    utilization is above the kink. A MarketState's ``s6`` is this rate at its own
    utilization.

    Args:
        model: ``p[11]``, the rationals ``[base, multiplier, jump, kink]``.
        util: The market utilization.

    Returns:
        The exact rate per interest period.

    Raises:
        ValueError: When the model does not have four terms.
    """
    if len(model) != _RATE_MODEL_TERMS:
        raise ValueError(f"rate model has {len(model)} terms, expected 4")
    base, multiplier, jump, kink = (_fraction(term) for term in model)
    rate = base + multiplier * util
    if util > kink:
        rate += jump * (util - kink)
    return rate


def borrow_apy(rate: Fraction, period_ms: int = INTEREST_PERIOD_MS) -> Decimal:
    """Yearly borrow rate, ``(1 + rate) ** (YEAR_MS / period_ms) - 1``.

    Args:
        rate: Borrow rate per interest period (``s6``).
        period_ms: The interest period ``p[10]`` in milliseconds.

    Returns:
        The compounded yearly rate to ``APY_PRECISION`` significant digits.

    Raises:
        ValueError: On a non-positive period or a negative rate.
    """
    if period_ms <= 0:
        raise ValueError("non-positive interest period")
    if rate < 0:
        raise ValueError("negative borrow rate")
    with localcontext() as ctx:
        ctx.prec = APY_PRECISION
        periods = Decimal(YEAR_MS) / Decimal(period_ms)
        return (1 + _decimal(rate)) ** periods - 1


def supply_apy(
    rate: Fraction,
    util: Fraction,
    income_ratio: Sequence[int],
    period_ms: int = INTEREST_PERIOD_MS,
) -> Decimal:
    """Yearly supply rate, ``borrow_apy * util * p[2][1] / p[2][0]``.

    The suppliers' share of the interest borrowers pay. Yield earned off chain (such
    as staking rewards on a market's idle Ada) is not part of it.

    Args:
        rate: Borrow rate per interest period (``s6``).
        util: The market utilization.
        income_ratio: ``p[2]``, ``[total, supplier share, ...]``.
        period_ms: The interest period ``p[10]`` in milliseconds.

    Returns:
        The yearly supply rate to ``APY_PRECISION`` significant digits.

    Raises:
        ValueError: On a malformed income ratio, a non-positive period or a negative
            rate.
    """
    share = _supplier_share(income_ratio)
    apy = borrow_apy(rate, period_ms)
    with localcontext() as ctx:
        ctx.prec = APY_PRECISION
        return apy * _decimal(util * share)


def apy_fraction(apy: Decimal) -> Fraction:
    """A Decimal yearly rate as the exact Fraction of its decimal digits."""
    return Fraction(str(apy))


def qtoken_rate(state: MarketStateDatum, income_ratio: Sequence[int]) -> Fraction:
    """Underlying per qToken, ``(s0 + s3 + floor(s4 * p[2][1] / p[2][0])) / s2``.

    Cash and principal plus the suppliers' share of the outstanding interest, over
    the qTokens in circulation: the value a batch records as ``s9``. A market with no
    qTokens in circulation keeps its recorded rate.

    Args:
        state: The market's MarketState record.
        income_ratio: ``p[2]``, ``[total, supplier share, ...]``.

    Returns:
        The exact qToken rate.

    Raises:
        ValueError: On a malformed income ratio.
    """
    _supplier_share(income_ratio)
    if state.qtokens <= 0:
        return _fraction(state.qtoken_rate)
    supplier_interest = floor_div(state.interest * income_ratio[1], income_ratio[0])
    return Fraction(state.cash + state.principal + supplier_interest, state.qtokens)


def supply_mint(amount: int, rate: Fraction) -> int:
    """Number of qTokens minted for ``amount`` base units, ``floor(amount / s9)``.

    Raises:
        ValueError: On a negative amount or a non-positive rate.
    """
    if rate <= 0:
        raise ValueError("non-positive qToken rate")
    if amount < 0:
        raise ValueError("negative supply amount")
    return floor_div(amount * rate.denominator, rate.numerator)


def min_interest_at_borrow(principal: int, min_rate: Fraction) -> int:
    """Minimum interest a borrow of ``principal`` records, ``ceil(principal * p[15])``.

    Raises:
        ValueError: On a negative minimum interest rate.
    """
    if min_rate < 0:
        raise ValueError("negative minimum interest rate")
    return ceil_div(principal * min_rate.numerator, min_rate.denominator)


def liquidation_debt(loan: LoanDatum, market_index: int) -> int:
    """Unfloored debt, ``max(ceil((principal + interest) * s5 / loan index), owed)``.

    The debt health and liquidation size are measured against. Interest accrues
    only through the market index, which moves at batch time, so the debt is exact
    as of the market's latest batch. A loan rebased after that batch carries an
    index at or above ``s5``; its indexed debt can round below ``principal +
    interest``, and the protocol then settles at ``principal + interest`` (the
    clamp). A loan index of 0 carries no accrual.

    Args:
        loan: The loan record.
        market_index: ``s5`` of the loan market's MarketState.

    Returns:
        The debt in base units of the borrowed asset.
    """
    owed = loan.principal + loan.interest
    if loan.index <= 0:
        return owed
    return max(ceil_div(owed * market_index, loan.index), owed)


def payoff(loan: LoanDatum, market_index: int) -> int:
    """Amount that repays the loan in full.

    The unfloored debt (``liquidation_debt``), floored at ``principal +
    min_interest``.

    Args:
        loan: The loan record.
        market_index: ``s5`` of the loan market's MarketState.

    Returns:
        The repayment in base units of the borrowed asset.
    """
    return max(
        liquidation_debt(loan, market_index),
        loan.principal + loan.min_interest,
    )


def liquidation_weight(weight: Rational, liquidation_threshold: Rational) -> Fraction:
    """A collateral's weight in the health factor, ``w * p[18]``.

    Args:
        weight: The collateral's CollateralParams weight ``w``.
        liquidation_threshold: The borrow market's liquidation threshold ``p[18]``
            (1 on every live market).

    Returns:
        The exact weight a holding's value counts with toward health.
    """
    return _fraction(weight) * _fraction(liquidation_threshold)


@dataclass(frozen=True)
class WeightedHolding:
    """One collateral of a loan, valued for health and LTV.

    Attributes:
        amount: Receipt tokens (qTokens) held, in base units.
        rate: Underlying per receipt token (the collateral market's ``s9``).
        price: The underlying's price per base unit, in the numeraire the borrowed
            asset's price is given in.
        weight: The weight the caller's health rule applies to the value: the
            CollateralParams weight ``w`` for the loan validator's health check
            on a build, or ``liquidation_weight`` (``w`` times ``p[18]``) for
            the liquidation health factor.
    """

    amount: int
    rate: Fraction
    price: Fraction
    weight: Fraction

    @property
    def value(self) -> Fraction:
        """Unweighted value, ``amount * rate * price``."""
        return self.amount * self.rate * self.price

    @property
    def weighted_value(self) -> Fraction:
        """Value counted toward health, ``value * weight``."""
        return self.value * self.weight


def _check_price(borrow_price: Fraction) -> None:
    if borrow_price <= 0:
        raise ValueError("non-positive borrow price")


def health(
    holdings: Sequence[WeightedHolding],
    debt: int,
    borrow_price: Fraction,
) -> Fraction:
    """Liqwid health factor, ``sum(weighted value) / (debt * borrow_price)``.

    Below 1 the loan can be liquidated.

    Args:
        holdings: The loan's collateral.
        debt: The unfloored debt (``liquidation_debt``) in borrowed base units.
        borrow_price: The borrowed asset's price per base unit, in the holdings'
            numeraire.

    Returns:
        The exact health factor.

    Raises:
        ValueError: When the debt or the borrow price is not positive.
    """
    if debt <= 0:
        raise ValueError("non-positive debt has no health factor")
    _check_price(borrow_price)
    weighted = sum((holding.weighted_value for holding in holdings), Fraction(0))
    return weighted / (debt * borrow_price)


def ltv(
    holdings: Sequence[WeightedHolding],
    debt: int,
    borrow_price: Fraction,
) -> Fraction:
    """Loan-to-value, ``debt * borrow_price / sum(unweighted value)``.

    Args:
        holdings: The loan's collateral.
        debt: The unfloored debt (``liquidation_debt``) in borrowed base units.
        borrow_price: The borrowed asset's price per base unit, in the holdings'
            numeraire.

    Returns:
        The exact loan-to-value.

    Raises:
        ValueError: On a negative debt, a non-positive borrow price or collateral
            without value.
    """
    if debt < 0:
        raise ValueError("negative debt")
    _check_price(borrow_price)
    value = sum((holding.value for holding in holdings), Fraction(0))
    if value <= 0:
        raise ValueError("collateral has no value")
    return debt * borrow_price / value


def liquidation_repay(
    loan: LoanDatum,
    market_index: int,
    close_factor: Fraction,
) -> int:
    """Most one liquidation may repay, ``floor(p[16] * liquidation_debt)``.

    Args:
        loan: The loan record.
        market_index: ``s5`` of the loan market's MarketState.
        close_factor: ``p[16]`` of the loan market (1/2, or 1 on full-liquidation
            markets).

    Returns:
        The repayment in base units of the borrowed asset.

    Raises:
        ValueError: When the close factor is outside (0, 1].
    """
    if not 0 < close_factor <= 1:
        raise ValueError(f"close factor {close_factor} outside (0, 1]")
    debt = liquidation_debt(loan, market_index)
    return floor_div(debt * close_factor.numerator, close_factor.denominator)


def seized_value(repay: int, discount: Fraction) -> Fraction:
    """Collateral value a liquidator takes for ``repay``, ``repay / discount``.

    Args:
        repay: Amount repaid, in base units of the borrowed asset.
        discount: CollateralParams field 2, the repaid share of the seized value
            (9/10 seizes 10/9 of the repayment).

    Returns:
        The seized value in base units of the borrowed asset.

    Raises:
        ValueError: On a non-positive discount.
    """
    if discount <= 0:
        raise ValueError("non-positive liquidation discount")
    return Fraction(repay) / discount


def seized_qtokens(
    repay: int,
    *,
    borrow_price: Fraction,
    discount: Fraction,
    rate: Fraction,
    price: Fraction,
) -> int:
    """QTokens of one collateral a liquidator takes for ``repay``.

    ``floor(repay * borrow_price / (discount * rate * price))``: the seized value
    (``seized_value``) priced in the borrowed asset, converted to the collateral's
    underlying and then to its qTokens, rounded down. It is the most the protocol
    lets a liquidation seize.

    ``discount`` is ``CollateralParamsDatum.liquidation_discount``, field 2 of the
    CollateralParams record (the repaid share of the seized value, e.g. 9/10). It
    is never ``CollateralTerms.liquidation_discount``, which is ``1 - field 2``:
    passing that one seizes nine times the legal amount at 9/10.

    Args:
        repay: Amount repaid, in base units of the borrowed asset.
        borrow_price: The borrowed asset's price per base unit.
        discount: CollateralParams field 2 of the seized collateral.
        rate: Underlying per qToken of the collateral market (its ``s9``).
        price: The collateral underlying's price per base unit, in the numeraire
            ``borrow_price`` is given in.

    Returns:
        The seized qTokens in base units.

    Raises:
        ValueError: When any input is not positive.
    """
    if repay <= 0:
        raise ValueError("non-positive repay")
    _check_price(borrow_price)
    value = seized_value(repay, discount)
    if rate <= 0:
        raise ValueError("non-positive rate")
    if price <= 0:
        raise ValueError("non-positive price")
    seized = value * borrow_price / (rate * price)
    return floor_div(seized.numerator, seized.denominator)
