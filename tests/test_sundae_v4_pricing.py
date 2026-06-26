"""Pricing tests for the SundaeSwap V4 invariant-module classes.

The bar for the constant-sum class is the on-chain validator's exact integer
relation: a constant-sum swap step pins the pool's value increase to
``floor(input_value * fee_num / fee_den)`` (``lib/modules/cs_check.ak``
``check_swap``). ``get_amount_out`` must therefore return the UNIQUE maximum
output that passes that bracket on a live pool — one unit more over-pays (fails
the tightness bound), one unit less over-charges (fails the achievable bound).

These tests decode the two live preview constant-sum pool datums for their real
3-asset reserves + ``total_lp``, project a 2-asset leg, and replay the on-chain
``check_swap`` relation against the projected output across several input sizes.
The concentrated-liquidity and constant-product classes get sanity coverage
(monotonicity, fee wedge, capacity cap, and — for CL — that the explicit-``L``
``virtual_reserves`` override is actually consulted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CLState
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CPPState
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CSState

_FIX = json.loads(
    (Path(__file__).parent / "sundae_v4_preview_fixtures.json").read_text(),
)

# The live off-datum constant-sum module configs (public on-chain values from the
# preview deployment manifest), keyed by pool identifier. Prices are 1:1:1; the
# fee differs per pool.
_CS_CONFIG = {
    "b8a0ec4e93e12799696095d4f50cbd8ffed0be04edad55e472f82945": {
        "prices": [1, 1, 1],
        "fee_num": 12,
        "fee_den": 1000,
    },
    "bd19fc1ac2be99322290374608130515fb91b1fa3a413d80a12f0a9f": {
        "prices": [1, 1, 1],
        "fee_num": 8,
        "fee_den": 1000,
    },
}


def _reserves(datum: SundaeV4PoolDatum) -> list[tuple[str, int]]:
    """Return ``[(unit, reserve), ...]`` in the datum's declared asset order."""
    out: list[tuple[str, int]] = []
    for entry in datum.assets:
        asset_class = entry[0]  # CBORTag(121, [policy, name])
        policy, name = asset_class.value[0], asset_class.value[1]
        unit = (policy.hex() + name.hex()) or "lovelace"
        out.append((unit, int(entry[1])))
    return out


def _cs_pools() -> list[dict]:
    """Decode the live constant-sum pools into ``{reserves, total_lp, config}``."""
    pools = []
    for record in _FIX["pool_datums"]:
        datum = SundaeV4PoolDatum.from_cbor(record["datum_hex"])
        ident = datum.identifier.hex()
        if ident not in _CS_CONFIG:
            continue
        pools.append(
            {
                "ident": ident,
                "reserves": _reserves(datum),
                "total_lp": datum.total_lp,
                "config": _CS_CONFIG[ident],
            },
        )
    return pools


def _cs_check_swap(
    before: list[int],
    after: list[int],
    before_lp: int,
    after_lp: int,
    fee_budget: int,
    prices: list[int],
    fee_num: int,
    fee_den: int,
) -> bool:
    """Faithful replay of ``cs_check.ak`` ``check_swap`` on aligned reserve vectors.

    ``before``/``after``/``prices`` are positionally aligned (the on-chain
    ``compute_deltas`` walk). Returns whether the step satisfies the achievable +
    tight value bracket and the LP-budget bracket.
    """
    if any(amt < 0 for amt in after):  # amt_after >= 0 guard
        return False
    v_b = sum(amt * p for amt, p in zip(before, prices))
    v_a = sum(amt * p for amt, p in zip(after, prices))
    input_value = sum(
        (aa - ab) * p for ab, aa, p in zip(before, after, prices) if aa > ab
    )
    has_inc = any(aa > ab for ab, aa in zip(before, after))
    has_dec = any(aa < ab for ab, aa in zip(before, after))
    v_increase = v_a - v_b
    return (
        has_inc
        and has_dec
        and fee_budget >= 0
        # fee-exact on the value delta (the part get_amount_out determines)
        and v_increase * fee_den <= input_value * fee_num
        and (v_increase + 1) * fee_den > input_value * fee_num
        # LP-budget tightness
        and (after_lp + fee_budget) * v_b <= v_a * before_lp
        and (after_lp + fee_budget + 1) * v_b > v_a * before_lp
    )


def _full_step_passes(
    pool: dict,
    in_idx: int,
    out_idx: int,
    amount_in: int,
    amount_out: int,
) -> bool:
    """Replay the full 3-asset on-chain step for a single (in, out) leg swap."""
    reserves = pool["reserves"]
    prices = pool["config"]["prices"]
    total_lp = pool["total_lp"]
    fee_num, fee_den = pool["config"]["fee_num"], pool["config"]["fee_den"]

    before = [r for _, r in reserves]
    after = list(before)
    after[in_idx] += amount_in
    after[out_idx] -= amount_out

    v_b = sum(amt * p for amt, p in zip(before, prices))
    v_a = sum(amt * p for amt, p in zip(after, prices))
    v_increase = v_a - v_b
    # The scooper picks fee_budget == floor(before_lp * v_increase / V_b); LP supply
    # is unchanged across a swap step (the fee accrues as value to existing LPs).
    fee_budget = (total_lp * v_increase) // v_b if v_b else 0
    return _cs_check_swap(
        before,
        after,
        total_lp,
        total_lp,
        fee_budget,
        prices,
        fee_num,
        fee_den,
    )


def _build_cs_leg(pool: dict, in_idx: int, out_idx: int) -> _SundaeV4CSState:
    """Project a 2-asset constant-sum leg state for the (in, out) asset pair."""
    reserves = pool["reserves"]
    in_unit, in_res = reserves[in_idx]
    out_unit, out_res = reserves[out_idx]
    cfg = pool["config"]
    return _SundaeV4CSState.model_validate(
        {
            "assets": Assets(**{in_unit: in_res, out_unit: out_res}),
            "block_time": 0,
            "block_index": 0,
            "plutus_v2": True,
            "datum_cbor": "00",
            "datum_hash": "00",
            "tx_index": 0,
            "tx_hash": "00",
            # prices aligned to (unit_a, unit_b) — 1:1 here, order-independent
            "price_a": 1,
            "price_b": 1,
            "fee_numerator": cfg["fee_num"],
            "fee_denominator": cfg["fee_den"],
        },
    )


_INPUT_SIZES = [1, 7, 100, 999, 1000, 1001, 250_000, 12_345_678]


@pytest.mark.parametrize("pool", _cs_pools(), ids=lambda p: p["ident"][:8])
def test_cs_get_amount_out_is_unique_max_passing_bracket(pool: dict) -> None:
    """get_amount_out lands EXACTLY on the cs_check achievable+tight bracket.

    For both swap directions of a projected leg and several input sizes: the
    quoted output passes the full on-chain step, output+1 fails (over-pay) and
    output-1 fails (over-charge) — i.e. the quote is the unique max that passes.
    """
    n = len(pool["reserves"])
    pairs = [(i, j) for i in range(n) for j in range(n) if i != j]
    for in_idx, out_idx in pairs:
        leg = _build_cs_leg(pool, in_idx, out_idx)
        in_unit = pool["reserves"][in_idx][0]
        for dx in _INPUT_SIZES:
            out, impact = leg.get_amount_out(Assets(**{in_unit: dx}))
            dy = out.quantity()
            assert dy > 0
            # The only wedge on a constant-sum leg is the fee; the realized impact is
            # the floored-fee fraction (0 when a tiny input floors the fee away), and
            # is bounded above by the nominal fee fraction.
            fee_frac = pool["config"]["fee_num"] / pool["config"]["fee_den"]
            assert impact == pytest.approx(1.0 - dy / dx, rel=1e-9)
            assert 0.0 <= impact <= fee_frac + 1e-9
            assert _full_step_passes(pool, in_idx, out_idx, dx, dy)
            assert not _full_step_passes(pool, in_idx, out_idx, dx, dy + 1)
            assert not _full_step_passes(pool, in_idx, out_idx, dx, dy - 1)


@pytest.mark.parametrize("pool", _cs_pools(), ids=lambda p: p["ident"][:8])
def test_cs_one_to_one_fee_formula(pool: dict) -> None:
    """At 1:1 prices the output is dx - floor(dx * fee_num / fee_den)."""
    leg = _build_cs_leg(pool, 0, 1)
    in_unit = pool["reserves"][0][0]
    fee_num, fee_den = pool["config"]["fee_num"], pool["config"]["fee_den"]
    for dx in _INPUT_SIZES:
        out, _ = leg.get_amount_out(Assets(**{in_unit: dx}))
        assert out.quantity() == dx - (dx * fee_num) // fee_den


@pytest.mark.parametrize("pool", _cs_pools(), ids=lambda p: p["ident"][:8])
def test_cs_output_capped_at_reserve(pool: dict) -> None:
    """An over-large input is capped at the out-side reserve, never exceeds it."""
    leg = _build_cs_leg(pool, 0, 1)
    in_unit, _ = pool["reserves"][0]
    out_unit, out_res = pool["reserves"][1]
    out, _ = leg.get_amount_out(Assets(**{in_unit: 10**18}))
    assert out.unit() == out_unit
    assert out.quantity() == out_res


@pytest.mark.parametrize("pool", _cs_pools(), ids=lambda p: p["ident"][:8])
def test_cs_get_amount_in_round_trips(pool: dict) -> None:
    """get_amount_in returns the minimal input that yields at least the output."""
    leg = _build_cs_leg(pool, 0, 1)
    out_unit = pool["reserves"][1][0]
    for desired in [1, 5, 1000, 987, 654_321]:
        amount_in, _ = leg.get_amount_in(Assets(**{out_unit: desired}))
        produced, _ = leg.get_amount_out(Assets(**{leg.unit_a: amount_in.quantity()}))
        # the chosen input covers the request ...
        assert produced.quantity() >= desired
        # ... and is minimal: one unit less does not
        smaller, _ = leg.get_amount_out(
            Assets(**{leg.unit_a: amount_in.quantity() - 1}),
        )
        assert smaller.quantity() < desired


def test_cs_monotonic_below_cap() -> None:
    """More input yields strictly more output below the reserve cap."""
    pool = _cs_pools()[0]
    leg = _build_cs_leg(pool, 0, 1)
    in_unit = pool["reserves"][0][0]
    o1, _ = leg.get_amount_out(Assets(**{in_unit: 1_000_000}))
    o2, _ = leg.get_amount_out(Assets(**{in_unit: 2_000_000}))
    assert o1.quantity() < o2.quantity() <= pool["reserves"][1][1]


# ---------------------------------------------------------------------------
# Constant-product reuse sanity
# ---------------------------------------------------------------------------

_CPP_ROW = {
    "assets": Assets(
        **{
            "d8906ca5c7ba124a0407a32dab37b2c82b13b3dcd9111e42940dcea45553444378": (
                615_372_926
            ),
            "45df5f274b8950b512b08d10656864958659c4ecf3ffad092ef6302455534472": (
                615_589_671
            ),
        },
    ),
    "block_time": 0,
    "block_index": 0,
    "plutus_v2": True,
    "datum_cbor": "00",
    "datum_hash": "00",
    "tx_index": 0,
    "tx_hash": "00",
}


def test_cpp_reuse_monotonic_and_fee_reduces_output() -> None:
    """The constant-product leg reuses x*y=k: monotonic, fee lowers output."""
    unit_in = "d8906ca5c7ba124a0407a32dab37b2c82b13b3dcd9111e42940dcea45553444378"
    free = _SundaeV4CPPState.model_validate({**_CPP_ROW, "fee": 0, "fee_basis": 10000})
    fee = _SundaeV4CPPState.model_validate({**_CPP_ROW, "fee": 30, "fee_basis": 10000})
    o_free, _ = free.get_amount_out(Assets(**{unit_in: 1_000_000}))
    o_fee, _ = fee.get_amount_out(Assets(**{unit_in: 1_000_000}))
    o_fee_big, _ = fee.get_amount_out(Assets(**{unit_in: 2_000_000}))
    assert o_fee.quantity() < o_free.quantity()
    assert o_fee.quantity() < o_fee_big.quantity()


# ---------------------------------------------------------------------------
# Concentrated-liquidity explicit-L override
# ---------------------------------------------------------------------------

_CL_ROW = {
    **_CPP_ROW,
    "fee": 50,
    "fee_basis": 10000,
    # off-curve reserves on a [sqrt(0.95), sqrt(1.05)]-ish band so reconstruction
    # and explicit-L disagree
    "sqrt_price_a_num": 95,
    "sqrt_price_a_den": 100,
    "sqrt_price_b_num": 105,
    "sqrt_price_b_den": 100,
}


def test_cl_virtual_reserves_consume_explicit_l() -> None:
    """The override uses total_lp directly (a_v = a + L/sqrt(Pb), b_v = b + L*sqrt(Pa))."""
    liq = 500_000_000
    cl = _SundaeV4CLState.model_validate({**_CL_ROW, "total_lp": liq})
    a, b = cl.reserve_a, cl.reserve_b
    a_v, b_v = cl.virtual_reserves()
    assert a_v == a + -(-(liq * 100) // 105)  # ceil(L / sqrt(Pb))
    assert b_v == b + -(-(liq * 95) // 100)  # ceil(L * sqrt(Pa))
    # Different explicit L => different virtual reserves (override is consulted)
    cl2 = _SundaeV4CLState.model_validate({**_CL_ROW, "total_lp": liq * 3})
    assert cl2.virtual_reserves() != (a_v, b_v)


def test_cl_output_capped_and_monotonic() -> None:
    """The CL leg is monotonic below the band edge and capped at the out reserve."""
    cl = _SundaeV4CLState.model_validate({**_CL_ROW, "total_lp": 500_000_000})
    unit_in = cl.unit_a
    o1, _ = cl.get_amount_out(Assets(**{unit_in: 1_000_000}))
    o2, _ = cl.get_amount_out(Assets(**{unit_in: 2_000_000}))
    assert 0 < o1.quantity() < o2.quantity()
    big, _ = cl.get_amount_out(Assets(**{unit_in: 10**18}))
    assert big.quantity() == cl.reserve_b
