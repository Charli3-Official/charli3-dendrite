"""Straight port of the stableswap validator checks (``ss_math.ak`` / ``ss_check.ak``).

Every predicate is integer-exact and mirrors the Aiken source clause for clause.
Reserves and rates are positionally aligned to the pool's declaration order; scaled
reserves are ``reserve * rate * 10^12``.
"""

from __future__ import annotations

P = 10**12


def exchange_invariant(
    new_gives: int, old_takes: int, raw: int, amp: int, old_d: int
) -> bool:
    """``y`` (old_takes - raw) is the smallest integer with 4A(x+y)+D >= 4AD + D^3/(4xy)."""
    new_takes = old_takes - raw
    d_cubed = old_d**3
    four_xy = 4 * new_gives * new_takes
    x_plus_y = new_gives + new_takes
    four_a = 4 * amp
    g1 = four_xy * (four_a * x_plus_y + old_d) - four_xy * four_a * old_d - d_cubed
    shifted = four_xy - 4 * new_gives
    g2 = (
        shifted * (four_a * (x_plus_y - 1) + old_d) - shifted * four_a * old_d - d_cubed
    )
    return g1 >= 0 and g2 < 0


def liquidity_invariant(x: int, y: int, amp: int, d: int) -> bool:
    """``d`` is the largest integer with 4A(x+y)+D >= 4AD + D^3/(4xy)."""
    four_a = 4 * amp
    four_xy = 4 * x * y
    sixteen_a_xy = four_a * four_xy
    f1 = sixteen_a_xy * d + d**3 - (sixteen_a_xy * (x + y) + four_xy * d)
    f2 = (
        sixteen_a_xy * (d + 1)
        + (d + 1) ** 3
        - (sixteen_a_xy * (x + y) + four_xy * (d + 1))
    )
    return f1 <= 0 and f2 > 0


def check_swap(
    before: list[int],
    after: list[int],
    rates: list[int],
    raw_swap_result: int,
    d_before: int,
    next_d: int,
    amp: int,
    fee: tuple[int, int],
) -> bool:
    """Whether the tag-3 check admits the reserve move ``before -> after``."""
    if any(a < 0 for a in after):
        return False
    if after[0] > before[0] and after[1] < before[1]:
        i, o = 0, 1
    elif after[1] > before[1] and after[0] < before[0]:
        i, o = 1, 0
    else:
        return False
    out_scale = rates[o] * P
    gross = raw_swap_result // out_scale
    takes = before[o] - after[o]
    fee_num, fee_den = fee
    return (
        gross - takes == (gross * fee_num + fee_den - 1) // fee_den
        and exchange_invariant(
            after[i] * rates[i] * P,
            before[o] * out_scale,
            raw_swap_result,
            amp,
            d_before,
        )
        and liquidity_invariant(
            after[i] * rates[i] * P, after[o] * out_scale, amp, next_d
        )
    )


def pinned(amt_b: int, amt_a: int, d_b: int, t: int) -> bool:
    """Whether one reserve moved in lock-step with the declared invariant change ``t``."""
    delta = amt_a - amt_b
    return delta * d_b >= amt_b * t and delta * d_b < amt_b * t + d_b


def check_liquidity(
    before: list[int],
    before_lp: int,
    after: list[int],
    after_lp: int,
    t: int,
    next_d: int,
    rates: list[int],
    amp: int,
    d_before: int,
) -> bool:
    """Whether the tag-4 / tag-6 check admits the pinned move (``t`` < 0 / > 0)."""
    return (
        t != 0
        and all(a >= 0 for a in after)
        and all(pinned(b, a, d_before, t) for b, a in zip(before, after))
        and (d_before + t) * before_lp >= d_before * after_lp
        and (d_before + t) * before_lp < d_before * (after_lp + 1)
        and liquidity_invariant(
            after[0] * rates[0] * P, after[1] * rates[1] * P, amp, next_d
        )
    )
