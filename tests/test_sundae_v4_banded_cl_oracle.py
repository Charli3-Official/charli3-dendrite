"""The banded CL math against the validator's own checks, on randomised ladders.

Two independent references:

* ``tests/sundae_v4_bcl_oracle.py``, a clause-for-clause Python port of
  ``banded_cl_check.ak`` (the *check* the chain runs on a declared step, not the
  quote arithmetic);
* the recorded fixture ``sundae_v4_banded_cl_vectors.json``, whose every swap step
  and liquidity move was also run through the real Aiken ``banded_step_ix`` in
  the sundae-v4 contracts repository (``v4-pool-perf`` at ``b620a7e``, aiken
  v1.1.24): 1,100 generated tests, 1,100 passed — 480 swap steps accepted and
  their one-more-unit-out variants rejected, 35 deposits and 35 withdrawals
  accepted and their short / greedy variants rejected. The generator
  (``scripts/gen_sundae_v4_banded_cl_vectors.py``) is deterministic and emits
  that Aiken module alongside the fixture, so the run can be repeated.

The fixture covers 35 ladders of 1 to 9 bands with mixed CL arcs and
constant-sum bins, uneven weights (1 to 199), asymmetric and zero fees, counters
from 10^6 to 10^15, and 116 swaps that cross at least one band edge. The tests
replay it, then run fresh seeded random states through the Python oracle, and
pin each rounding rule of the spec's table (§5.1) in isolation.
"""

from __future__ import annotations

import importlib.util
import json
import random
from collections.abc import Iterator
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4BandedCLPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import banded_cl_pinned_deposit
from charli3_dendrite.dexs.amm.sundae_v4 import banded_cl_pinned_withdraw
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import TWO64
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Band
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import BandState
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Ladder
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import achievable
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import band_output
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import banded_quote
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import ceil_div
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import find_witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import is_witness
from tests import sundae_v4_bcl_oracle as oracle
from tests.sundae_v4_vault_factory import build_banded_cl_vault_utxo

_HERE = Path(__file__).parent
FIXTURE = json.loads((_HERE / "sundae_v4_banded_cl_vectors.json").read_text())
_GEN_PATH = _HERE.parent / "scripts" / "gen_sundae_v4_banded_cl_vectors.py"


def _generator():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("bcl_gen", _GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pairs(items: list) -> list[tuple[int, int]]:
    return [(int(p), int(q)) for p, q in items]


def _ladder(rec: dict) -> Ladder:
    return Ladder.from_shape(
        starts=_pairs(rec["starts"]),
        closing=tuple(rec["closing"]),
        weights=list(rec["weights"]),
        curves=list(rec["curves"]),
        fees_buy=_pairs(rec["fees_buy"]),
        fees_sell=_pairs(rec["fees_sell"]),
        weight_total=rec["weight_total"],
    )


def _oracle_bands(rec: dict) -> list[dict]:
    return [
        dict(start=tuple(s), weight=w, curve=c, fee_buy=tuple(fb), fee_sell=tuple(fs))
        for s, w, c, fb, fs in zip(
            rec["starts"],
            rec["weights"],
            rec["curves"],
            rec["fees_buy"],
            rec["fees_sell"],
        )
    ]


def _step_ok(rec: dict, st: dict, lp: int, after: list[int], lp_after: int) -> bool:
    return oracle.banded_step(
        tuple(st["before"]),
        st["x"],
        st["k"],
        lp,
        tuple(after),
        st["x_after"],
        lp_after,
        st["fee_budget"],
        _oracle_bands(rec),
        tuple(rec["closing"]),
        rec["weight_total"],
    )


# ── the fixture itself ────────────────────────────────────────────────────────


def test_fixture_covers_the_shapes_the_chain_vectors_do_not() -> None:
    ladders = FIXTURE["ladders"]
    swaps = FIXTURE["swaps"]
    assert len(ladders) >= 30
    assert len(swaps) >= 250
    assert {len(l["starts"]) for l in ladders} >= {1, 2, 3, 4, 5, 8, 9}
    assert sum(1 for l in ladders if len(set(l["weights"])) > 1) >= 20
    assert sum(1 for l in ladders if l["fees_buy"] != l["fees_sell"]) >= 30
    assert any([0, 1] in l["fees_buy"] or [0, 1] in l["fees_sell"] for l in ladders)
    steps = [(ladders[s["ladder"]], st) for s in swaps for st in s["steps"]]
    assert len(steps) >= 450
    assert sum(1 for lad, st in steps if lad["curves"][st["k"]] == 1) >= 150
    assert sum(1 for lad, st in steps if lad["curves"][st["k"]] == 0) >= 150
    assert sum(1 for s in swaps if len(s["steps"]) > 1) >= 100
    assert sum(1 for s in swaps if s["a_is_input"]) >= 100
    assert sum(1 for s in swaps if not s["a_is_input"]) >= 100
    counters = [s["witness"][0] for s in swaps]
    assert min(counters) < 10**7 and max(counters) > 10**14
    assert all(lad["index"] == [list(e) for e in _ladder(lad).index] for lad in ladders)
    assert FIXTURE["rejected"]["oracle_fail"] == 0
    assert FIXTURE["rejected"]["unpriceable_state"] == 0


@pytest.mark.parametrize("index", range(len(FIXTURE["swaps"])))
def test_recorded_swap_replays_and_passes_the_validator_check(index: int) -> None:
    sw = FIXTURE["swaps"][index]
    rec = FIXTURE["ladders"][sw["ladder"]]
    ladder = _ladder(rec)
    a, b = sw["reserves"]
    witness = find_witness(ladder, a, b)
    assert witness is not None
    assert [witness.counter, witness.band] == sw["witness"]
    quote = banded_quote(ladder, a, b, sw["dx"], sw["a_is_input"])
    assert quote.amount_out == sw["amount_out"]
    assert quote.spent == sw["spent"]
    assert list(quote.bands) == sw["bands"]
    assert [(s.amount_in, s.amount_out) for s in quote.steps] == [
        (st["amount_in"], st["amount_out"]) for st in sw["steps"]
    ]
    for st in sw["steps"]:
        assert _step_ok(rec, st, sw["lp"], st["after"], sw["lp"])
        a1, b1 = st["after"]
        greedy = [a1, b1 - 1] if sw["a_is_input"] else [a1 - 1, b1]
        assert not _step_ok(rec, st, sw["lp"], greedy, sw["lp"])
        assert st["fee_budget"] >= 0
        assert st["x_after"] >= st["x"]


@pytest.mark.parametrize("index", range(len(FIXTURE["liquidity"])))
def test_recorded_liquidity_moves_are_the_pinned_ones_and_pass(index: int) -> None:
    lq = FIXTURE["liquidity"][index]
    rec = FIXTURE["ladders"][lq["ladder"]]
    a, b = lq["reserves"]
    st = dict(
        before=[a, b], x=lq["witness"][0], k=lq["witness"][1], x_after=0, fee_budget=0
    )
    assert lq["deposit_ok"] and lq["withdraw_ok"]
    assert _step_ok(rec, st, lq["lp"], lq["deposit_after"], lq["lp_after"])
    a1, b1 = lq["deposit_after"]
    assert not _step_ok(rec, st, lq["lp"], [a1 - 1, b1], lq["lp_after"])
    assert not _step_ok(rec, st, lq["lp"], [a1, b1 - 1], lq["lp_after"])
    assert not _step_ok(rec, st, lq["lp"], [a1, b1], lq["lp_after"] + 1)
    pinned = banded_cl_pinned_deposit([a, b], lq["offered"], lq["lp"])
    assert [a + pinned.deltas[0], b + pinned.deltas[1]] == lq["deposit_after"]
    assert pinned.lp_after == lq["lp_after"]
    lp_after = lq["lp"] - lq["burn"]
    assert _step_ok(rec, st, lq["lp"], lq["withdraw_after"], lp_after)
    a1, b1 = lq["withdraw_after"]
    assert not _step_ok(rec, st, lq["lp"], [a1 - 1, b1], lp_after)
    assert not _step_ok(rec, st, lq["lp"], [a1, b1 - 1], lp_after)
    withdraw = banded_cl_pinned_withdraw([a, b], lq["burn"], lq["lp"])
    assert [a - withdraw.payouts[0], b - withdraw.payouts[1]] == lq["withdraw_after"]
    assert withdraw.lp_after == lp_after


def test_recorded_counter_drops_are_edge_crossings_the_quote_still_prices() -> None:
    """An edge crossing out of a tiny-weight bin can re-derive a lower counter.

    The quote's output is the chain's for the step, but the budget pair needs
    ``fee_budget >= 0`` (a non-falling counter) within one transcript, so such a
    crossing cannot continue in the same scoop. Recorded, rare, and priced.
    """
    drops = FIXTURE["counter_drops"]
    steps = sum(len(s["steps"]) for s in FIXTURE["swaps"])
    assert len(drops) * 100 <= steps  # under 1% of steps
    for d in drops:
        rec = FIXTURE["ladders"][d["ladder"]]
        ladder = _ladder(rec)
        assert d["x_after"] < d["x"]
        assert d["k_after"] != d["k"]
        assert rec["curves"][d["k"]] == 1
        assert rec["weights"][d["k"]] * 50 < rec["weight_total"]
        a0, b0 = d["before"]
        quote = banded_quote(ladder, a0, b0, d["amount_in"], d["a_is_input"])
        assert quote.steps[0].amount_out == d["amount_out"]
        # Priced correctly as a step of its own: the output pair accepts it and
        # one more unit fails, with the counter held (no budget to pay).
        st = dict(before=d["before"], x=d["x"], k=d["k"], x_after=d["x"], fee_budget=0)
        assert _step_ok(rec, st, d["x"], d["after"], d["x"])
        a1, b1 = d["after"]
        greedy = [a1, b1 - 1] if d["a_is_input"] else [a1 - 1, b1]
        assert not _step_ok(rec, st, d["x"], greedy, d["x"])


# ── the pool type on the same vectors ────────────────────────────────────────


@pytest.fixture()
def _preview() -> Iterator[None]:
    SundaeV4Vault.select_network("preview")
    SundaeV4Vault.clear_config_cache()
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_config_cache()


@pytest.mark.usefixtures("_preview")
@pytest.mark.parametrize("index", range(0, len(FIXTURE["swaps"]), 7))
def test_pool_type_quotes_the_recorded_vectors(index: int) -> None:
    sw = FIXTURE["swaps"][index]
    rec = FIXTURE["ladders"][sw["ladder"]]
    a_unit, b_unit = "01" * 28 + "0a", "02" * 28 + "0b"
    a, b = sw["reserves"]
    values, config = build_banded_cl_vault_utxo(
        [(a_unit, a), (b_unit, b)],
        starts=_pairs(rec["starts"]),
        closing=tuple(rec["closing"]),
        weights=list(rec["weights"]),
        curves=list(rec["curves"]),
        fee_buy=_pairs(rec["fees_buy"]),
        fee_sell=_pairs(rec["fees_sell"]),
        total_lp=sw["lp"],
    )
    vault = SundaeV4Vault.model_validate(values)
    vault.supply_module_config(
        SundaeV4Deployment.for_network("preview").banded_cl_hash, config
    )
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4BandedCLPool)
    unit_in, unit_out = (a_unit, b_unit) if sw["a_is_input"] else (b_unit, a_unit)
    out, _ = pool.get_amount_out(Assets(**{unit_in: sw["dx"]}), unit_out)
    assert out.quantity() == sw["amount_out"]
    assert pool.active_band == sw["witness"][1]
    if sw["amount_out"] > 0 and sw["spent"] == sw["dx"]:
        needed, _ = pool.get_amount_in(Assets(**{unit_out: sw["amount_out"]}), unit_in)
        assert needed.quantity() <= sw["dx"]
        assert pool.quote(unit_in, needed.quantity()).amount_out >= sw["amount_out"]


# ── fresh random states through the oracle ───────────────────────────────────


def test_fresh_random_states_pass_the_oracle_and_fail_greedily() -> None:
    gen = _generator()
    rng = random.Random(777)
    checked = inside = crossings = 0
    while checked < 120:
        shape = gen.rand_ladder(rng)
        ladder = Ladder.from_shape(**shape)
        state = gen.rand_state(rng, ladder)
        if state is None:
            continue
        a, b = state
        witness = find_witness(ladder, a, b)
        assert witness is not None, (shape, state)
        # Tight: achievable at X, not at X + 1; a witness nowhere else nearby.
        assert achievable(ladder, a, b, witness.counter, witness.band)
        assert not achievable(ladder, a, b, witness.counter + 1, witness.band)
        assert not is_witness(ladder, a, b, witness.counter - 1, witness.band)
        st = witness.state
        if 0 < witness.ra < st.a_sat and 0 < witness.rb < st.b_sat:
            inside += 1
            # Strictly inside one band: no other band admits a witness.
            for k in range(ladder.n):
                if k != witness.band:
                    assert find_witness(ladder, a, b, prefer=k).band == witness.band
        bands = gen.oracle_bands(shape)
        lp = witness.counter
        for a_in in (True, False):
            cap = b if a_in else a
            dx = max(1, int(cap * rng.choice([0.001, 0.3, 0.9, 2.5])))
            quote = banded_quote(ladder, a, b, dx, a_in)
            ca, cb = a, b
            assert quote.spent <= dx
            if quote.spent < dx:
                # Stopped early: nothing more could be bought from the end state.
                ea, eb = quote.reserves_after
                assert (
                    banded_quote(ladder, ea, eb, dx - quote.spent, a_in).amount_out == 0
                )
            for i, step in enumerate(quote.steps):
                assert step.amount_out > 0
                w = step.witness
                na, nb = (
                    (ca + step.amount_in, cb - step.amount_out)
                    if a_in
                    else (ca - step.amount_out, cb + step.amount_in)
                )
                # The output pair with the counter held: pure pricing check.
                ok = oracle.banded_step(
                    (ca, cb),
                    w.counter,
                    w.band,
                    lp,
                    (na, nb),
                    w.counter,
                    lp,
                    0,
                    bands,
                    shape["closing"],
                    shape["weight_total"],
                )
                assert ok, (shape, (ca, cb), w, step)
                greedy = (na, nb - 1) if a_in else (na - 1, nb)
                assert not oracle.banded_step(
                    (ca, cb),
                    w.counter,
                    w.band,
                    lp,
                    greedy,
                    w.counter,
                    lp,
                    0,
                    bands,
                    shape["closing"],
                    shape["weight_total"],
                )
                # The chain's capacity rule: the after residual is non-negative,
                # and when the step crossed, one more unit of input would not fit.
                cap_left = (nb - w.state.cb) if a_in else (na - w.state.ca)
                assert cap_left >= 0
                if i + 1 < len(quote.steps):
                    crossings += 1
                    over = band_output(
                        w, ladder.bands[w.band], a_in, step.amount_in + 1
                    )
                    assert over > (w.rb if a_in else w.ra)
                ca, cb = na, nb
        checked += 1
    assert inside >= 40
    assert crossings >= 40


# ── each rounding rule of the spec's table, in isolation ─────────────────────


def _one_band(lo: tuple, hi: tuple, curve: int = 0, fee: tuple = (0, 1)) -> Ladder:
    return Ladder.from_shape(
        starts=[lo],
        closing=hi,
        weights=[1],
        curves=[curve],
        fees_buy=[fee],
        fees_sell=[fee],
        weight_total=1,
    )


def test_r3_r4_saturation_is_one_division_ceiled_not_via_the_floored_l() -> None:
    """Spec §2.2 counterexample: w=1, X=109, W=10, edges 1/1 and 2/1."""
    ladder = Ladder.from_shape(
        starts=[(1, 1)] + [(2 + i, 1) for i in range(9)],
        closing=(11, 1),
        weights=[1] * 10,
        curves=[0] * 10,
        fees_buy=[(0, 1)] * 10,
        fees_sell=[(0, 1)] * 10,
        weight_total=10,
    )
    st = ladder.at(109, 0)
    assert st.liquidity == 10  # floor(109 / 10)
    assert (
        st.a_sat == 6
    )  # ceil(109 * 1 / (10 * 1 * 2)) = ceil(5.45), NOT ceil(10 / 2) = 5
    assert st.b_sat == 11  # ceil(109 / 10)


def test_r2_active_liquidity_floors() -> None:
    ladder = Ladder.from_shape(
        starts=[(1, 1), (2, 1)],
        closing=(3, 1),
        weights=[3, 7],
        curves=[0, 0],
        fees_buy=[(0, 1)] * 2,
        fees_sell=[(0, 1)] * 2,
        weight_total=10,
    )
    assert ladder.at(1_000_000_009, 1).liquidity == 700_000_006  # floor(7 * X / 10)
    assert ladder.at(1_000_000_009, 0).liquidity == 300_000_002


def test_r5_r6_prefix_sums_round_up_once_from_rounded_up_coefficients() -> None:
    ladder = Ladder.from_shape(
        starts=[(1, 1), (2, 1), (3, 1)],
        closing=(4, 1),
        weights=[1, 1, 1],
        curves=[0, 0, 0],
        fees_buy=[(0, 1)] * 3,
        fees_sell=[(0, 1)] * 3,
        weight_total=3,
    )
    # coeff_a_i = ceil(w D 2^64 / (W p_{i-1} p_i)): band 1 -> 2^64/(3*2*3), band 2 -> 2^64/(3*3*4)
    exact_a1, exact_a2 = TWO64 / 18, TWO64 / 36
    ca0 = ladder.index[0][0]
    assert ca0 == ceil_div(TWO64, 18) + ceil_div(TWO64, 36)
    assert ca0 > exact_a1 + exact_a2
    x = 10**9 + 7
    st = ladder.at(x, 0)
    assert st.ca == ceil_div(ca0 * x, TWO64)
    assert st.ca * TWO64 >= ca0 * x > (st.ca - 1) * TWO64
    # The composed overshoot stays under two base units (spec §5 composed bound 1).
    exact = x * (exact_a1 + exact_a2) / TWO64
    assert exact <= st.ca < exact + 2
    assert ladder.index[2][0] == 0 and ladder.index[0][1] == 0


def _witness_at(ladder: Ladder, k: int, x: int, ra: int, rb: int) -> Witness:
    st = ladder.at(x, k)
    return Witness(counter=x, band=k, state=st, ra=ra, rb=rb)


def test_r12_fee_floors_on_the_input_and_picks_the_direction() -> None:
    band = Band(
        lo=(1, 1), hi=(2, 1), weight=1, curve=1, fee_buy=(0, 1), fee_sell=(7, 997)
    )
    ladder = Ladder(bands=(band,), weight_total=1, index=((0, 0),))
    w = _witness_at(ladder, 0, 10**9, 10**8, 10**8)
    # Selling A pays fee_sell: fee = floor(1000 * 7 / 997) = 7, dx_eff = 993, bin at 2/1.
    assert band_output(w, band, True, 1000) == 993 * 2
    # Buying A pays fee_buy, which is zero here.
    assert band_output(w, band, False, 1000) == 500
    # Whole fee on a one-unit input: floor(1 * 7 / 997) = 0, so nothing is lost.
    assert band_output(w, band, True, 1) == 2


def test_r19_constant_sum_outputs_floor_in_both_directions() -> None:
    band = Band(
        lo=(1, 2), hi=(3, 1), weight=1, curve=1, fee_buy=(0, 1), fee_sell=(0, 1)
    )
    ladder = Ladder(bands=(band,), weight_total=1, index=((0, 0),))
    w = _witness_at(ladder, 0, 10**9, 10**8, 10**8)
    # Pn = 1*3 = 3, Pd = 2*1 = 2: price 3/2 B per A.
    assert band_output(w, band, True, 5) == 7  # floor(5 * 3 / 2) = 7.5 -> 7
    assert band_output(w, band, False, 5) == 3  # floor(5 * 2 / 3) = 3.33 -> 3
    # The validator's pair admits exactly these and rejects one more.
    assert oracle.cs_output_pair(
        10**8, 10**8, 10**8 + 5, 10**8 - 7, True, 5, (1, 2), (3, 1)
    )
    assert not oracle.cs_output_pair(
        10**8, 10**8, 10**8 + 5, 10**8 - 8, True, 5, (1, 2), (3, 1)
    )
    assert oracle.cs_output_pair(
        10**8, 10**8, 10**8 - 3, 10**8 + 5, False, 5, (1, 2), (3, 1)
    )
    assert not oracle.cs_output_pair(
        10**8, 10**8, 10**8 - 4, 10**8 + 5, False, 5, (1, 2), (3, 1)
    )


def test_r13_cl_output_floors_and_the_pair_rejects_one_more() -> None:
    ladder = _one_band((1, 1), (2, 1))
    x = 10**9
    st = ladder.at(x, 0)
    # A state inside the band: ra = a_sat / 3, rb the smallest reachable.
    ra = st.a_sat // 3
    lo, hi = 0, st.b_sat
    while lo < hi:
        mid = (lo + hi) // 2
        if (ra * 2 + st.liquidity) * (mid + st.liquidity) - st.liquidity**2 * 2 >= 0:
            hi = mid
        else:
            lo = mid + 1
    rb = lo
    w = find_witness(ladder, ra, rb)
    assert w is not None and w.band == 0
    va, vb = w.ra * 2 + w.state.liquidity, w.rb + w.state.liquidity
    dx = 12_345
    out = band_output(w, ladder.bands[0], True, dx)
    assert out == vb * (dx * 2) // ((va + dx * 2) * 1)
    assert out < vb * (dx * 2) / (va + dx * 2)
    assert oracle.swap_output_pair(ra, rb, ra + dx, rb - out, va, vb, dx, 1, 2)
    assert not oracle.swap_output_pair(ra, rb, ra + dx, rb - out - 1, va, vb, dx, 1, 2)


def test_r15_budget_pair_floors_the_fee_budget() -> None:
    x0, x1, lp = 1_000_049_919, 1_000_049_930, 1_000_049_689
    bp = lp * x1 // x0
    assert oracle.budget_pair_form3(x0, lp, x1, bp)
    assert not oracle.budget_pair_form3(x0, lp, x1, bp + 1)
    assert not oracle.budget_pair_form3(x0, lp, x1, bp - 1)
    assert oracle.swap_fee_budget(x0, lp, x1, lp) == bp - lp >= 0


def test_r20_proportional_check_is_the_pinned_deposit_and_withdrawal() -> None:
    reserves, lp = [5_953_003, 6_249_988], 1_000_049_689
    dep = banded_cl_pinned_deposit(reserves, [100_000, 104_657], lp)
    after = [r + d for r, d in zip(reserves, dep.deltas)]
    assert oracle.check_proportional(reserves, after, lp, dep.lp_after)
    for i in range(2):
        short = list(after)
        short[i] -= 1
        assert not oracle.check_proportional(reserves, short, lp, dep.lp_after)
    assert not oracle.check_proportional(reserves, after, lp, dep.lp_after + 1)
    wd = banded_cl_pinned_withdraw(reserves, 50_000, lp)
    paid = [r - p for r, p in zip(reserves, wd.payouts)]
    assert oracle.check_proportional(reserves, paid, lp, wd.lp_after)
    for i in range(2):
        greedy = list(paid)
        greedy[i] -= 1
        assert not oracle.check_proportional(reserves, greedy, lp, wd.lp_after)


def test_the_state_derivation_matches_the_oracle_port_field_for_field() -> None:
    for rec in FIXTURE["ladders"][:12]:
        ladder = _ladder(rec)
        bands = _oracle_bands(rec)
        idx = oracle.build_index(bands, tuple(rec["closing"]), rec["weight_total"])
        assert idx == list(ladder.index)
        for x in (ladder.weight_total, 10**6 + 3, 10**12 + 11):
            for k in range(ladder.n):
                st: BandState = ladder.at(x, k)
                ost = oracle.ladder_at_upper(
                    bands, idx, tuple(rec["closing"]), rec["weight_total"], x, k
                )
                assert (st.ca, st.cb, st.a_sat, st.b_sat, st.liquidity) == (
                    ost.ca,
                    ost.cb,
                    ost.a_sat_k,
                    ost.b_sat_k,
                    ost.l_k,
                )
