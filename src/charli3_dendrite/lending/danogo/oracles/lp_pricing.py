"""Danogo robust LP-token pricing (confirmed from docs).

Prices an AMM LP token in a quote asset X from the pool reserves and the component
token prices, using the manipulation-resistant method Danogo documents in its
"Prevent LP Price Manipulation: Robust LP Pricing" blog. This is the leaf math for
the LP oracle source families (`TMinswapLP`, `TMinswapLPStable`, `TSplash*`).

Fair value (held by a balanced constant-product pool):

    price_LP = (reserve_a * price_a + reserve_b * price_b) / total_lp

Manipulation guard: the same value can be computed from EITHER side alone if the
pool is balanced and the external component prices are honest:

    price_LP_a = 2 * reserve_a * price_a / total_lp
    price_LP_b = 2 * reserve_b * price_b / total_lp

If these two diverge beyond a tolerance, the pool is imbalanced / being manipulated
and the LP price must not be trusted.

All component prices are exact rationals ``(num, denom)`` quoted in X, and results
are exact rationals, so callers can stay in integer arithmetic end to end.
"""

from __future__ import annotations

from charli3_dendrite.lending.math import BASIS

Rational = tuple[int, int]


def lp_token_price(
    *,
    reserve_a: int,
    price_a: Rational,
    reserve_b: int,
    price_b: Rational,
    total_lp: int,
) -> Rational:
    """Fair LP price in X as an exact rational ``(num, denom)``.

    ``(reserve_a*price_a + reserve_b*price_b) / total_lp``. Raises ValueError if
    ``total_lp`` or a component denominator is non-positive.
    """
    pa_num, pa_den = price_a
    pb_num, pb_den = price_b
    if total_lp <= 0 or pa_den <= 0 or pb_den <= 0:
        raise ValueError("total_lp and price denominators must be positive")
    num = reserve_a * pa_num * pb_den + reserve_b * pb_num * pa_den
    denom = total_lp * pa_den * pb_den
    return (num, denom)


def lp_side_prices(
    *,
    reserve_a: int,
    price_a: Rational,
    reserve_b: int,
    price_b: Rational,
    total_lp: int,
) -> tuple[Rational, Rational]:
    """The two single-side LP price estimates ``(price_LP_a, price_LP_b)`` in X."""
    pa_num, pa_den = price_a
    pb_num, pb_den = price_b
    if total_lp <= 0 or pa_den <= 0 or pb_den <= 0:
        raise ValueError("total_lp and price denominators must be positive")
    side_a = (2 * reserve_a * pa_num, total_lp * pa_den)
    side_b = (2 * reserve_b * pb_num, total_lp * pb_den)
    return side_a, side_b


def lp_price_is_consistent(
    *,
    reserve_a: int,
    price_a: Rational,
    reserve_b: int,
    price_b: Rational,
    total_lp: int,
    tolerance_bps: int = 500,
) -> bool:
    """True if the two single-side LP prices agree within ``tolerance_bps``.

    Compares the side estimates by relative deviation from their midpoint (exact
    integer cross-multiplication, no floats). ``tolerance_bps`` defaults to 500 (5%),
    matching Danogo's published oracle deviation band; the exact epsilon Danogo uses
    for the LP guard is not published, so this is configurable.
    """
    (a_num, a_den), (b_num, b_den) = lp_side_prices(
        reserve_a=reserve_a,
        price_a=price_a,
        reserve_b=reserve_b,
        price_b=price_b,
        total_lp=total_lp,
    )
    # |a-b| relative to midpoint (a+b)/2, all as integers over a_den*b_den:
    #   2*|a_num*b_den - b_num*a_den| / (a_num*b_den + b_num*a_den) <= tol_bps/BASIS
    cross_a = a_num * b_den
    cross_b = b_num * a_den
    total = cross_a + cross_b
    if total <= 0:
        return False
    return 2 * abs(cross_a - cross_b) * BASIS <= tolerance_bps * total
