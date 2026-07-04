"""FluidTokens loan math (perpetual / amortization / installments + health).

Ports `ft-cardano-loans-v3` ``lib/fluidtokens/finance.ak`` exactly: the contract works
in exact rationals (`aiken/math/rational`) and rounds the final amount with ``ceil``, so
this module uses :class:`fractions.Fraction` and :func:`math.ceil` to reproduce the
on-chain value bit-for-bit. ``interest_rate`` is the datum field scaled by 10000
(``rational.new(interestRate, 10000)`` on-chain); time is POSIX milliseconds and hours
are kept as exact fractions (the contract does NOT floor them).
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from math import ceil

_BASIS = 10000
_HOURS_PER_YEAR = 8760
_MS_PER_HOUR = 3_600_000


def _accumulated_installment_hours(
    repaid_installments: int,
    installment_period: int,
    initial_grace_period: int,
) -> int:
    """Hours covered by already-repaid installments (0 when none repaid)."""
    if repaid_installments == 0:
        return 0
    return initial_grace_period + installment_period * repaid_installments


def perpetual_outstanding_debt(
    *,
    principal: int,
    interest_rate: int,
    apy_coef: int,
    lend_date_ms: int,
    now_ms: int,
    repaid_installments: int = 0,
    installment_period: int = 0,
    initial_grace_period: int = 0,
) -> int:
    """Perpetual remaining debt == ``finance.ak`` get_remaining_debt (PerpetualLoan).

    ``remainingInterest = principal * (c * dh + m * H^2) / 8760`` where
    ``c = interest_rate/10000``, ``m = apy_coef/1_000_000``, ``H`` is the exact
    fractional hours since ``lend_date`` and
    ``dh = H - accumulatedHoursOfRepaidInstallments``; the debt is
    ``ceil(principal + remainingInterest)``. The on-chain validator measures time to the
    transaction's validity upper bound; analytics use ``now_ms`` as that proxy.
    """
    t = now_ms - lend_date_ms
    if t <= 0:
        return principal
    hours = Fraction(t, _MS_PER_HOUR)
    hours_squared = Fraction(t * t, _MS_PER_HOUR * _MS_PER_HOUR)
    m = Fraction(apy_coef, 1_000_000)
    c = Fraction(interest_rate, _BASIS)
    accumulated = _accumulated_installment_hours(
        repaid_installments,
        installment_period,
        initial_grace_period,
    )
    hours_since_last = hours - accumulated
    remaining_interest = (
        Fraction(principal) * (c * hours_since_last + m * hours_squared)
    ) / _HOURS_PER_YEAR
    return ceil(Fraction(principal) + remaining_interest)


def amortization_installment(
    *,
    principal: int,
    interest_rate: int,
    total_installments: int,
    is_late: bool = False,
    penalty_fee: int = 0,
) -> int:
    """Amortized installment == ``get_next_installment_amount`` (InterestOnRemaining).

    Per-installment rate is ``r = (interest_rate/10000) / total_installments`` (the
    contract divides the rate across installments); the annuity is
    ``ceil(P*r*(1+r)^n / ((1+r)^n - 1) + penalty)``. A zero rate would divide by zero
    on-chain; here it falls back to an even split (rather than raising).
    """
    if total_installments <= 0:
        return principal
    penalty = penalty_fee if is_late else 0
    rate = Fraction(interest_rate, _BASIS) / total_installments
    if rate == 0:
        return ceil(Fraction(principal, total_installments) + penalty)
    one = Fraction(1)
    rate_pow = (one + rate) ** total_installments
    numerator = Fraction(principal) * rate * rate_pow
    denominator = rate_pow - one
    return ceil(numerator / denominator + penalty)


def installments_pi_amount(
    *,
    principal: int,
    interest_rate: int,
    total_installments: int,
    is_late: bool = False,
    penalty_fee: int = 0,
) -> int:
    """(principal + total interest) / installments == PrincipalAndInterestInstallments.

    Total interest is ``principal * interest_rate/10000`` (computed upfront), split
    evenly across the installments and rounded up: ``ceil((P + P*r)/n + penalty)``.
    """
    if total_installments <= 0:
        return principal
    penalty = penalty_fee if is_late else 0
    total_interest = Fraction(principal) * Fraction(interest_rate, _BASIS)
    single = (Fraction(principal) + total_interest) / total_installments
    return ceil(single + penalty)


def perpetual_installment_amount(
    *,
    principal: int,
    interest_rate: int,
    installment_period: int,
    initial_grace_period: int,
    repaid_installments: int,
    is_late: bool = False,
    penalty_fee: int = 0,
) -> int:
    """Perpetual periodic installment (interest only) == PerpetualLoan branch.

    ``installmentAmount = principal * periodHours * (interest_rate/10000)/8760``
    where the first period also covers the initial grace period.
    """
    penalty = penalty_fee if is_late else 0
    period_hours = (
        initial_grace_period + installment_period
        if repaid_installments == 0
        else installment_period
    )
    hourly_rate = Fraction(interest_rate, _BASIS) / _HOURS_PER_YEAR
    amount = Fraction(principal) * period_hours * hourly_rate
    return ceil(amount + penalty)


def health_factor(
    *,
    collateral_value: Decimal,
    debt: Decimal,
    l_tv: int,
    l_tv_divider: int,
) -> Decimal:
    """(collateral_value * lTV) / (debt * divider); >=1 healthy, <1 liquidatable.

    The liquidation LTV threshold is ``l_tv / l_tv_divider`` (the fraction of
    collateral value a loan may borrow against), so the maximum supportable debt is
    ``collateral_value * l_tv / l_tv_divider`` and the health factor is that ceiling
    divided by the current debt. This mirrors the contract's ``can_liquidate`` (which
    liquidates when ``liquidationLtv < debt/collateral``).
    """
    # A non-positive divider is a degenerate/missing LTV config; treat as
    # infinitely healthy rather than raising, mirroring the debt<=0 guard.
    if debt <= 0 or l_tv_divider <= 0:
        return Decimal("Infinity")
    return (collateral_value * Decimal(l_tv)) / (debt * Decimal(l_tv_divider))
