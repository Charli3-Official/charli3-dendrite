"""Pricing tests for the SundaeSwap V4 invariant-module classes.

The bar for the constant-sum class is the on-chain validator's exact integer
relation: a constant-sum swap step pins the pool's value increase to a one
out-unit-wide window above ``floor(input_value * fee_num / fee_den)``
(``lib/modules/cs_check.ak`` ``check_swap``) and the LP budget to the value
increase net of the bounty dock. ``get_amount_out`` must therefore return the
UNIQUE maximum output that passes that bracket on a live pool — one unit more
over-pays (fails the tightness bound), one unit less over-charges (fails the
achievable bound).

These tests decode the live audit-final constant-sum pool datums for their real
reserves + ``total_lp``, take each pool's price weights / fee / bounty from the
module ``Create`` redeemer it was created with, project a 2-asset leg, and replay
the on-chain ``check_swap`` relation against the projected output across several
input sizes. The concentrated-liquidity and constant-product classes get sanity
coverage (monotonicity, fee wedge, capacity cap, and — for CL — that the
explicit-``L`` ``virtual_reserves`` override is actually consulted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumCreate
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CLState
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CPPState
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CSState

_FIX = json.loads(
    (Path(__file__).parent / "sundae_v4_auditfinal_fixtures.json").read_text(),
)


def _reserves(datum: SundaeV4PoolDatum) -> list[tuple[str, int]]:
    """Return ``[(unit, reserve), ...]`` in the datum's declared asset order."""
    out: list[tuple[str, int]] = []
    for entry in datum.assets:
        asset_class = AssetClass.from_primitive(entry[0])
        unit = (asset_class.policy.hex() + asset_class.asset_name.hex()) or "lovelace"
        out.append((unit, int(entry[1])))
    return out


def _cs_pools() -> list[dict]:
    """Decode the live constant-sum pools into ``{reserves, total_lp, config}``.

    The config is the exact one committed into the pool's ``module_state``: the
    first field of the constant-sum module's ``Create`` redeemer.
    """
    pools = []
    for record in _FIX["pool_datums"]:
        if "module_create_redeemers" not in record:
            continue
        datum = SundaeV4PoolDatum.from_cbor(bytes.fromhex(record["datum"]))
        create = ConstantSumCreate.from_cbor(
            bytes.fromhex(record["module_create_redeemers"]["constant_sum"]),
        )
        config = create.initial_state
        pools.append(
            {
                "ident": datum.identifier.hex(),
                "reserves": _reserves(datum),
                "total_lp": datum.total_lp,
                "config": {
                    "prices": list(config.prices),
                    "fee_num": config.fee.num,
                    "fee_den": config.fee.den,
                    "bounty_num": config.bounty_k.num,
                    "bounty_den": config.bounty_k.den,
                },
            },
        )
    return pools


def _dock(
    before: list[int],
    after: list[int],
    prices: list[int],
    bounty_num: int,
    bounty_den: int,
) -> int:
    """``cs_check.ak``'s bounty dock: the obligation a swap accrues, ceiled.

    ``ceil(max(0, B_after - B_before))`` in the validator's cross-multiplied
    integer form; zero when the bounty is off or the swap rebalances.
    """
    if bounty_num == 0:
        return 0
    n = len(prices)
    v_b = sum(amt * p for amt, p in zip(before, prices))
    v_a = sum(amt * p for amt, p in zip(after, prices))
    q_b = sum((n * amt * p - v_b) ** 2 for amt, p in zip(before, prices))
    q_a = sum((n * amt * p - v_a) ** 2 for amt, p in zip(after, prices))
    accrual = bounty_num * (q_a * v_b - q_b * v_a)
    if accrual <= 0:
        return 0
    den = bounty_den * n * n * v_a * v_b
    return (accrual + den - 1) // den


def _cs_check_swap(
    before: list[int],
    after: list[int],
    before_lp: int,
    after_lp: int,
    fee_budget: int,
    prices: list[int],
    fee_num: int,
    fee_den: int,
    dock: int,
) -> bool:
    """Faithful replay of ``cs_check.ak`` ``check_swap`` on aligned reserve vectors.

    ``before``/``after``/``prices`` are positionally aligned (the on-chain
    ``compute_deltas`` walk). Returns whether the step satisfies the one
    out-unit-wide value window and the docked LP-budget bracket.
    """
    if any(amt < 0 for amt in after):  # amt_after >= 0 guard
        return False
    v_b = sum(amt * p for amt, p in zip(before, prices))
    v_a = sum(amt * p for amt, p in zip(after, prices))
    input_value = sum(
        (aa - ab) * p for ab, aa, p in zip(before, after, prices) if aa > ab
    )
    decreased = [p for ab, aa, p in zip(before, after, prices) if aa < ab]
    has_inc = any(aa > ab for ab, aa in zip(before, after))
    has_dec = bool(decreased)
    p_out = min(decreased) if decreased else 0
    v_increase = v_a - v_b
    return (
        has_inc
        and has_dec
        and fee_budget >= 0
        # fee window on the value delta, one out-unit wide
        and (v_increase - p_out + 1) * fee_den <= input_value * fee_num
        and (v_increase + 1) * fee_den > input_value * fee_num
        # LP-budget tightness on the docked value
        and (after_lp + fee_budget) * v_b <= (v_a - dock) * before_lp
        and (after_lp + fee_budget + 1) * v_b > (v_a - dock) * before_lp
    )


def _full_step_passes(
    pool: dict,
    in_idx: int,
    out_idx: int,
    amount_in: int,
    amount_out: int,
) -> bool:
    """Replay the full on-chain step for a single (in, out) leg swap."""
    reserves = pool["reserves"]
    cfg = pool["config"]
    prices = cfg["prices"]
    total_lp = pool["total_lp"]

    before = [r for _, r in reserves]
    after = list(before)
    after[in_idx] += amount_in
    after[out_idx] -= amount_out

    v_b = sum(amt * p for amt, p in zip(before, prices))
    v_a = sum(amt * p for amt, p in zip(after, prices))
    dock = _dock(before, after, prices, cfg["bounty_num"], cfg["bounty_den"])
    # The scooper picks fee_budget so that after_lp + fee_budget lands on
    # floor(before_lp * (V_a - dock) / V_b); LP supply is held unchanged here.
    fee_budget = (total_lp * (v_a - dock)) // v_b - total_lp if v_b else 0
    return _cs_check_swap(
        before,
        after,
        total_lp,
        total_lp,
        fee_budget,
        prices,
        cfg["fee_num"],
        cfg["fee_den"],
        dock,
    )


def _dock_floor(
    pool: dict,
    in_idx: int,
    out_idx: int,
    amount_in: int,
    amount_out: int,
) -> bool:
    """Whether the bounty dock exceeds the step's fee for this (in, out) pair.

    An imbalancing swap whose floored fee is smaller than the obligation it
    accrues cannot satisfy ``fee_budget >= 0`` at any output — the dock is a
    minimum-trade-size floor on bounty-bearing pools.
    """
    cfg = pool["config"]
    prices = cfg["prices"]
    before = [r for _, r in pool["reserves"]]
    after = list(before)
    after[in_idx] += amount_in
    after[out_idx] -= amount_out
    v_b = sum(amt * p for amt, p in zip(before, prices))
    v_a = sum(amt * p for amt, p in zip(after, prices))
    dock = _dock(before, after, prices, cfg["bounty_num"], cfg["bounty_den"])
    return dock > v_a - v_b


def _build_cs_leg(pool: dict, in_idx: int, out_idx: int) -> _SundaeV4CSState:
    """Project a 2-asset constant-sum leg state for the (in, out) asset pair."""
    reserves = pool["reserves"]
    in_unit, in_res = reserves[in_idx]
    out_unit, out_res = reserves[out_idx]
    cfg = pool["config"]
    prices = {in_unit: cfg["prices"][in_idx], out_unit: cfg["prices"][out_idx]}
    unit_a, unit_b = sorted(prices)
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
            # price weights aligned to the leg's canonical (unit_a, unit_b)
            "price_a": prices[unit_a],
            "price_b": prices[unit_b],
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
            p_in, p_out = (
                pool["config"]["prices"][in_idx],
                pool["config"]["prices"][out_idx],
            )
            if dy == 0:
                # The fee-floored input value cannot afford one unit of a pricier
                # out asset; nothing is quoted and nothing could be paid.
                fee_value = (dx * p_in * pool["config"]["fee_num"]) // pool["config"][
                    "fee_den"
                ]
                assert dx * p_in - fee_value < p_out
                continue
            # The only wedges on a constant-sum leg are the fee and the whole-unit
            # rounding of the out asset: the realized impact is the floored-fee
            # fraction (0 when a tiny input floors the fee away) plus at most one
            # out-unit's worth of value, so it is bounded by the nominal fee plus
            # that rounding slack.
            fee_frac = pool["config"]["fee_num"] / pool["config"]["fee_den"]
            assert impact == pytest.approx(1.0 - (dy * p_out) / (dx * p_in), rel=1e-9)
            assert 0.0 <= impact <= fee_frac + p_out / (dx * p_in) + 1e-9
            if not _full_step_passes(pool, in_idx, out_idx, dx, dy):
                # Below the bounty dock's floor no output can pass; the quote is
                # still the value-window answer, just not executable at this size.
                assert _dock_floor(pool, in_idx, out_idx, dx, dy)
                assert not any(
                    _full_step_passes(pool, in_idx, out_idx, dx, candidate)
                    for candidate in range(max(1, dy - 3), dy + 4)
                )
                continue
            assert not _full_step_passes(pool, in_idx, out_idx, dx, dy + 1)
            assert not _full_step_passes(pool, in_idx, out_idx, dx, dy - 1)


@pytest.mark.parametrize("pool", _cs_pools(), ids=lambda p: p["ident"][:8])
def test_cs_value_conservation_formula(pool: dict) -> None:
    """The output is the fee-floored input value converted at the fixed prices."""
    leg = _build_cs_leg(pool, 0, 1)
    in_unit = pool["reserves"][0][0]
    p_in, p_out = pool["config"]["prices"][0], pool["config"]["prices"][1]
    fee_num, fee_den = pool["config"]["fee_num"], pool["config"]["fee_den"]
    for dx in _INPUT_SIZES:
        out, _ = leg.get_amount_out(Assets(**{in_unit: dx}))
        value = dx * p_in
        assert out.quantity() == (value - (value * fee_num) // fee_den) // p_out


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
