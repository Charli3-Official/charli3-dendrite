"""Exact SundaeSwap V4 stableswap math: the integers the validator admits.

The stableswap module prices two reserves on Curve's invariant

    4A(x + y) + D = 4AD + D^3 / (4xy)

over reserves scaled by their config rate and :data:`STABLESWAP_PRECISION`
(``x = reserve * rate * 10**12``). The validator solves nothing: it checks that a
step's declared ``D`` is the largest integer with ``f(D) <= 0`` and that its
post-swap out-side reserve is the smallest integer ``y`` with ``g(y) >= 0`` (``g`` is
``-f`` at the post-swap reserves). These functions port the contract's off-chain
reference: integer Newton iterations, then the one-step fix-up onto each bracket.
Every value is a Python int and every division has non-negative operands, where
floor division and Aiken's truncating division agree.
"""

from __future__ import annotations

from dataclasses import dataclass

STABLESWAP_PRECISION = 10**12
_MAX_ITERATIONS = 255


def _f(x: int, y: int, amp: int, d: int) -> int:
    """``16A·xy·D + D³ - 16A·xy·(x + y) - 4xy·D``: at most 0 up to the invariant."""
    sixteen_a_xy = 16 * amp * x * y
    return sixteen_a_xy * d + d * d * d - (sixteen_a_xy * (x + y) + 4 * x * y * d)


def _g(x: int, y: int, amp: int, d: int) -> int:
    """``4xy·(4A·(x + y) + D) - 4xy·4A·D - D³``: at least 0 on or above the curve."""
    four_xy = 4 * x * y
    return four_xy * (4 * amp * (x + y) + d) - four_xy * 4 * amp * d - d * d * d


def stableswap_d(amp: int, x: int, y: int) -> int:
    """The invariant ``D`` of scaled reserves ``x``, ``y`` (0 when a side is empty).

    Raises:
        ArithmeticError: the Newton iteration does not settle.
    """
    if x <= 0 or y <= 0:
        return 0
    total = x + y
    ann = 4 * amp
    d = total
    for _ in range(_MAX_ITERATIONS):
        d_p = d * d * d // (4 * x * y)
        d_next = (ann * total + 2 * d_p) * d // ((ann - 1) * d + 3 * d_p)
        settled = abs(d_next - d) <= 1
        d = d_next
        if settled:
            break
    else:
        msg = "stableswap D did not converge."
        raise ArithmeticError(msg)
    for _ in range(_MAX_ITERATIONS):
        if _f(x, y, amp, d) > 0:
            d -= 1
        elif _f(x, y, amp, d + 1) <= 0:
            d += 1
        else:
            return d
    msg = "stableswap D did not settle on its bracket."
    raise ArithmeticError(msg)


def stableswap_y(amp: int, d: int, x: int) -> int:
    """The smallest scaled out-side reserve on or above the curve at in-side ``x``.

    Raises:
        ArithmeticError: the Newton iteration does not settle.
    """
    ann = 4 * amp
    c = d * d // (2 * x) * d // (2 * ann)
    b = x + d // ann
    y = d
    for _ in range(_MAX_ITERATIONS):
        y_next = (y * y + c) // (2 * y + b - d)
        settled = abs(y_next - y) <= 1
        y = y_next
        if settled:
            break
    else:
        msg = "stableswap y did not converge."
        raise ArithmeticError(msg)
    for _ in range(_MAX_ITERATIONS):
        if _g(x, y, amp, d) < 0:
            y += 1
        elif _g(x, y - 1, amp, d) >= 0:
            y -= 1
        else:
            return y
    msg = "stableswap y did not settle on its bracket."
    raise ArithmeticError(msg)


@dataclass(frozen=True)
class StableSwapQuote:
    """One swap step as the validator pins it.

    ``raw_swap_result`` is the scaled out-side reserve delta, ``gross`` its token
    amount, ``fee`` the ceiled swap fee (it stays in the reserve) and ``takes`` what
    the trader receives.
    """

    raw_swap_result: int
    gross: int
    fee: int
    takes: int


def stableswap_swap(
    amp: int,
    d: int,
    reserve_in: int,
    rate_in: int,
    reserve_out: int,
    rate_out: int,
    amount_in: int,
    fee: tuple[int, int],
) -> StableSwapQuote:
    """The step offering ``amount_in`` against reserves whose invariant is ``d``.

    Reserves are token units, rates the config's, ``fee`` its ``(num, den)``.
    """
    scale_in = rate_in * STABLESWAP_PRECISION
    scale_out = rate_out * STABLESWAP_PRECISION
    y_after = stableswap_y(amp, d, (reserve_in + amount_in) * scale_in)
    raw = reserve_out * scale_out - y_after
    gross = raw // scale_out
    fee_num, fee_den = fee
    fee_amount = (gross * fee_num + fee_den - 1) // fee_den
    return StableSwapQuote(
        raw_swap_result=raw,
        gross=gross,
        fee=fee_amount,
        takes=gross - fee_amount,
    )
