"""FluidTokens loan math (perpetual / amortization / installments + health)."""

from __future__ import annotations

from decimal import Decimal

from charli3_dendrite.lending.math import BASIS
from charli3_dendrite.lending.math import bps_mul_floor
from charli3_dendrite.lending.math import ceil_div

_HOURS_PER_YEAR = 8760
_MS_PER_HOUR = 3_600_000


def _hours_elapsed(lend_date_ms: int, now_ms: int) -> int:
    if now_ms <= lend_date_ms:
        return 0
    return (now_ms - lend_date_ms) // _MS_PER_HOUR


def perpetual_outstanding_debt(
    *,
    principal: int,
    interest_rate: int,
    apy_coef: int,
    lend_date_ms: int,
    now_ms: int,
) -> int:
    """Outstanding debt for a perpetual loan: principal + accrued interest.

    APY(x) = apy_coef/1_000_000 * x + interest_rate/10000 (x = hours since lend).
    Interest accrues hourly on the constant principal. Returns smallest units.
    """
    h = _hours_elapsed(lend_date_ms, now_ms)
    if h == 0:
        return principal
    base = Decimal(interest_rate) / Decimal(BASIS)
    slope = Decimal(apy_coef) / Decimal(1_000_000)
    avg_apy = base + slope * Decimal(h) / 2
    interest = Decimal(principal) * avg_apy * Decimal(h) / Decimal(_HOURS_PER_YEAR)
    return principal + int(interest.to_integral_value(rounding="ROUND_FLOOR"))


def amortization_installment(
    *,
    principal: int,
    interest_rate: int,
    total_installments: int,
) -> int:
    """Constant installment = P * r / (1 - (1+r)^-n), r per-installment rate."""
    if total_installments <= 0:
        return principal
    r = Decimal(interest_rate) / Decimal(BASIS)
    if r == 0:
        return ceil_div(principal, total_installments)
    factor = Decimal(1) - (Decimal(1) + r) ** (-total_installments)
    value = Decimal(principal) * r / factor
    return int(value.to_integral_value(rounding="ROUND_CEILING"))


def installments_pi_amount(
    *,
    principal: int,
    interest_rate: int,
    total_installments: int,
) -> int:
    """(principal + total_interest) / installments (interest computed upfront)."""
    if total_installments <= 0:
        return principal
    interest = bps_mul_floor(principal, interest_rate)
    total = principal + interest
    return ceil_div(total, total_installments)


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
    divided by the current debt.
    """
    # A non-positive divider is a degenerate/missing LTV config; treat as
    # infinitely healthy rather than raising, mirroring the debt<=0 guard.
    if debt <= 0 or l_tv_divider <= 0:
        return Decimal("Infinity")
    return (collateral_value * Decimal(l_tv)) / (debt * Decimal(l_tv_divider))
