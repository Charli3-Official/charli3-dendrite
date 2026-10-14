"""Seeded random banded CL ladders and states, for the oracle tests.

Ladders have 1 to 9 bands mixing CL arcs and constant-sum bins, uneven weights (1
to 199), asymmetric and zero fees, and edges from wide to very narrow; states sit
inside a random band at a random counter from the weight total up to 10^15. The
same seed always yields the same ladders and states.
"""

from __future__ import annotations

import random

from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Ladder
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import curve_g


def rand_ladder(rng: random.Random) -> dict:
    """A random ladder shape, as :meth:`Ladder.from_shape` keyword arguments."""
    n = rng.choice([1, 1, 2, 3, 4, 5, 8, 9])
    den = rng.choice([1, 10, 1000, 80000, 10**6])
    nums = [rng.randint(1, 5 * den)]
    for _ in range(n):
        step = max(1, den // rng.choice([1, 4, 40, 400]))
        nums.append(nums[-1] + rng.randint(1, step))
    weights = [rng.choice([1, 1, 2, 3, 7, 100, 199]) for _ in range(n)]
    return {
        "starts": [(p, den) for p in nums[:-1]],
        "closing": (nums[-1], den),
        "weights": weights,
        "curves": [rng.choice([0, 0, 1]) for _ in range(n)],
        "fees_buy": [
            rng.choice([(0, 1), (3, 1000), (1, 100), (5, 10000), (7, 997)])
            for _ in range(n)
        ],
        "fees_sell": [
            rng.choice([(0, 1), (3, 1000), (25, 10000), (1, 10), (11, 1009)])
            for _ in range(n)
        ],
        "weight_total": sum(weights),
    }


def rand_state(rng: random.Random, ladder: Ladder) -> tuple[int, int] | None:
    """Reserves on a random band's own curve at a random counter, or ``None``."""
    k = rng.randrange(ladder.n)
    counter = rng.randint(
        ladder.weight_total,
        rng.choice([10**6, 10**9, 10**12, 10**15]),
    )
    state = ladder.at(counter, k)
    band = ladder.bands[k]
    if state.a_sat <= 0 or state.b_sat <= 0:
        return None
    ra = rng.randint(0, state.a_sat)
    lo, hi = 0, state.b_sat + 1
    if curve_g(band, ra, hi, state.liquidity) < 0:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if curve_g(band, ra, mid, state.liquidity) >= 0:
            hi = mid
        else:
            lo = mid + 1
    if lo > state.b_sat:
        return None
    return (ra + state.ca, lo + state.cb)


def oracle_bands(shape: dict) -> list[dict]:
    """A shape's bands in the oracle's plain-dict form."""
    return [
        {"start": s, "weight": w, "curve": c, "fee_buy": fb, "fee_sell": fs}
        for s, w, c, fb, fs in zip(
            shape["starts"],
            shape["weights"],
            shape["curves"],
            shape["fees_buy"],
            shape["fees_sell"],
        )
    ]


def random_cases(seed: int, count: int) -> list[tuple[dict, Ladder, tuple[int, int]]]:
    """``count`` priceable ``(shape, ladder, reserves)`` cases drawn from ``seed``."""
    rng = random.Random(seed)
    cases = []
    while len(cases) < count:
        shape = rand_ladder(rng)
        ladder = Ladder.from_shape(**shape)
        state = rand_state(rng, ladder)
        if state is not None:
            cases.append((shape, ladder, state))
    return cases
