"""Unit tests for the Dano CLMM single-range capacity behaviour (backend-free).

A Dano pool UTxO is a single concentrated-liquidity range holding a finite
amount of each token. A swap that would push the price past the range's
``[sqrt_lower, sqrt_upper]`` band cannot be filled by this UTxO. Rather than
raising, ``get_amount_out`` returns the range's *capacity* (the real output
reserve) — the maximum obtainable output — mirroring the order-book
convention (``ob_base`` caps at ``available``). Callers route the remainder
to other ranges; the minimal input for the capped output comes from
``get_amount_in``.
"""

from __future__ import annotations

import json
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.dano import DanoCLMMState

_FIX = json.loads(
    (Path(__file__).parent / "data" / "dano_pool_fixtures.json").read_text()
)


def _pool(key: str) -> DanoCLMMState:
    return DanoCLMMState.model_validate(_FIX[key]["row"])


def test_get_amount_out_caps_at_band_edge() -> None:
    s = _pool("both_reserve")
    cap = s.reserve_b  # X->Y is bounded by the real Y reserve
    unit_x = s._datum.unit_x

    # below the cap: a normal swap is well-defined and strictly under capacity
    small_out, _ = s.get_amount_out(Assets(root={unit_x: 1_000_000}))
    assert 0 < small_out.quantity() < cap

    # above the cap: returns the capacity instead of raising
    big_out, _ = s.get_amount_out(Assets(root={unit_x: 10**13}))
    assert big_out.quantity() == cap


def test_get_amount_out_zero_reserve_returns_zero() -> None:
    # a range parked at its band edge has one reserve == 0; output that side is 0
    s = _pool("zero_reserve")
    assert s.reserve_b == 0
    out, _ = s.get_amount_out(Assets(root={s._datum.unit_x: 10**9}))
    assert out.quantity() == 0


def test_get_amount_out_monotonic_below_cap() -> None:
    s = _pool("both_reserve")
    unit_x = s._datum.unit_x
    o1, _ = s.get_amount_out(Assets(root={unit_x: 1_000_000}))
    o2, _ = s.get_amount_out(Assets(root={unit_x: 2_000_000}))
    assert o1.quantity() < o2.quantity() <= s.reserve_b
