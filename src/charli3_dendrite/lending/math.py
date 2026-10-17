"""Exact-integer helpers mirroring on-chain validator arithmetic.

Lending validators use integer floor/ceil division and basis-point math.
Reproducing that exactly matters near `health_factor == 1`, where a single
unit of rounding flips a loan between healthy and liquidatable.
"""

from __future__ import annotations

from fractions import Fraction
from math import floor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

BASIS = 10_000  # basis points: 10_000 bps = 100%


def floor_div(num: int, denom: int) -> int:
    """Floor of num / denom (rounds toward negative infinity)."""
    if denom == 0:
        raise ZeroDivisionError("denominator is zero")
    return num // denom


def ceil_div(num: int, denom: int) -> int:
    """Ceiling of num / denom for integer inputs."""
    if denom == 0:
        raise ZeroDivisionError("denominator is zero")
    return -((-num) // denom)


def bps_mul_floor(value: int, bps: int) -> int:
    """floor(value * bps / BASIS)."""
    return floor_div(value * bps, BASIS)


def bps_mul_ceil(value: int, bps: int) -> int:
    """ceil(value * bps / BASIS)."""
    return ceil_div(value * bps, BASIS)


def borrow_headroom(available: int, principal: int, cap: Fraction) -> int:
    """Most that can still be lent before principal exceeds ``cap`` of the funds.

    The funds are what is still available plus what is already lent, so lending ``x``
    more keeps ``principal + x <= cap * (available + principal)``. Floored, and 0 when
    principal already meets or exceeds the cap. The result is not bounded by
    ``available``; callers that lend from ``available`` take the smaller of the two.
    """
    room = cap * (available + principal) - principal
    return max(0, floor_div(room.numerator, room.denominator))


def apportion(total: int, weights: Sequence[Fraction]) -> list[int]:
    """Split ``total`` in proportion to non-negative ``weights``, summing exactly.

    Each part is its share floored; the units the floors leave go one each to the
    largest remainders, ties to the lower index (the largest-remainder method), so a
    zero weight always gets 0 and every part is within one unit of its exact share.

    Raises:
        ValueError: ``weights`` is empty, holds a negative weight or only zeros, or
            ``total`` is negative.
    """
    if not weights:
        raise ValueError("apportion needs at least one weight")
    if total < 0:
        raise ValueError(f"cannot apportion a negative total: {total}")
    if any(weight < 0 for weight in weights):
        raise ValueError("weights must be non-negative")
    weight_sum = sum(weights, Fraction(0))
    if weight_sum == 0:
        raise ValueError("weights must not all be zero")
    shares = [Fraction(total) * weight / weight_sum for weight in weights]
    parts = [floor(share) for share in shares]
    left = total - sum(parts)
    by_remainder = sorted(
        range(len(shares)),
        key=lambda index: (parts[index] - shares[index], index),
    )
    for index in by_remainder[:left]:
        parts[index] += 1
    return parts
