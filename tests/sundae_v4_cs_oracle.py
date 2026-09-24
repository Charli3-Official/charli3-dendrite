"""Straight port of the constant-sum validator checks (``cs_check.ak``).

Reserve vectors are positionally aligned to ``prices`` (the on-chain
declaration order). Every predicate is integer-exact and mirrors the Aiken
source clause for clause; the swap check assumes LP is unchanged by the step
and the scooper declares the tight ``fee_budget`` the validator brackets.
"""

from __future__ import annotations


def compute_v(reserves: list[int], prices: list[int]) -> int:
    """The pool's total value: the reserve/price dot product."""
    return sum(r * p for r, p in zip(reserves, prices))


def compute_q(reserves: list[int], prices: list[int], v: int) -> int:
    """The pool's imbalance: the sum of squared per-asset value deviations."""
    n = len(prices)
    return sum((n * r * p - v) ** 2 for r, p in zip(reserves, prices))


def dock(
    before: list[int], after: list[int], prices: list[int], bounty_k: tuple[int, int]
) -> int:
    """The obligation a step accrues toward the rebalance bounty (0 if none)."""
    k_num, k_den = bounty_k
    if k_num == 0:
        return 0
    n = len(prices)
    v_b, v_a = compute_v(before, prices), compute_v(after, prices)
    q_b, q_a = compute_q(before, prices, v_b), compute_q(after, prices, v_a)
    accrual = k_num * (q_a * v_b - q_b * v_a)
    if accrual <= 0:
        return 0
    den = k_den * n * n * v_a * v_b
    return (accrual + den - 1) // den


def check_swap(
    before: list[int],
    after: list[int],
    before_lp: int,
    prices: list[int],
    fee: tuple[int, int],
    bounty_k: tuple[int, int],
) -> bool:
    """Whether the on-chain tag-3 predicate admits the step's tight ``fee_budget``."""
    if len(before) != len(after) or any(a < 0 for a in after):
        return False
    fee_num, fee_den = fee
    v_b, v_a = compute_v(before, prices), compute_v(after, prices)
    input_value = sum((a - b) * p for b, a, p in zip(before, after, prices) if a > b)
    decreased = [p for b, a, p in zip(before, after, prices) if a < b]
    has_inc = any(a > b for b, a in zip(before, after))
    has_dec = bool(decreased)
    p_out = min(decreased) if decreased else 0
    v_increase = v_a - v_b
    d = dock(before, after, prices, bounty_k)
    after_lp = before_lp
    fee_budget = (before_lp * (v_a - d)) // v_b - after_lp
    return (
        has_inc
        and has_dec
        and fee_budget >= 0
        and (v_increase - p_out + 1) * fee_den <= input_value * fee_num
        and (v_increase + 1) * fee_den > input_value * fee_num
        and (after_lp + fee_budget) * v_b <= (v_a - d) * before_lp
        and (after_lp + fee_budget + 1) * v_b > (v_a - d) * before_lp
    )


def check_pinned_reserves(
    before: list[int], after: list[int], v_b: int, t: int
) -> bool:
    """Whether every reserve moved in lock-step with the value change ``t``."""
    return all(
        (a - b) * v_b >= b * t and (a - b) * v_b < b * t + v_b
        for b, a in zip(before, after)
    )


def check_deposit(
    before: list[int],
    before_lp: int,
    after: list[int],
    after_lp: int,
    t: int,
    prices: list[int],
) -> bool:
    """Whether the on-chain deposit predicate admits raising every reserve by ``t``."""
    v_b = compute_v(before, prices)
    return (
        t > 0
        and check_pinned_reserves(before, after, v_b, t)
        and (v_b + t) * before_lp >= v_b * after_lp
        and (v_b + t) * before_lp < v_b * (after_lp + 1)
    )


def check_withdraw(
    before: list[int],
    before_lp: int,
    after: list[int],
    after_lp: int,
    t: int,
    prices: list[int],
) -> bool:
    """Whether the on-chain withdraw predicate admits lowering every reserve by ``t``."""
    v_b = compute_v(before, prices)
    return (
        t < 0
        and all(a >= 0 for a in after)
        and check_pinned_reserves(before, after, v_b, t)
        and (v_b + t) * before_lp >= v_b * after_lp
        and (v_b + t) * before_lp < v_b * (after_lp + 1)
    )
