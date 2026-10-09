"""The banded CL math against the validator's own checks, on seeded random ladders.

The reference is ``tests/sundae_v4_bcl_oracle.py``, a clause-for-clause Python
port of ``banded_cl_check.ak``: the *checks* the chain runs on a declared
transcript, not the quote arithmetic. Ladders and states come from
``tests/sundae_v4_bcl_ladders.py``, seeded, so every run checks the same cases:
1 to 9 bands mixing CL arcs and constant-sum bins, uneven weights (1 to 199),
asymmetric and zero fees, and counters from the weight total to 10^15.

Every quote is checked as the transcript one scoop would declare: its steps'
witnesses threaded through the walk (each step's band proof, output pair and
budget pair, so no counter falls), and a final boundary bounded at a counter no
lower than the last step's. One more unit out of any step fails. Each rounding
rule of the spec's table (§5.1) is also pinned in isolation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4BandedCLPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import banded_cl_pinned_deposit
from charli3_dendrite.dexs.amm.sundae_v4 import banded_cl_pinned_withdraw
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import TWO64
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Band
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import BandedQuote
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import BandState
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Ladder
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import achievable
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import band_output
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import banded_quote
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import directed_witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import find_witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import is_witness
from charli3_dendrite.lending.math import ceil_div

from tests import sundae_v4_bcl_oracle as oracle
from tests.sundae_v4_bcl_ladders import oracle_bands
from tests.sundae_v4_bcl_ladders import random_cases
from tests.sundae_v4_vault_factory import build_banded_cl_vault_utxo
from tests.test_sundae_v4_banded_cl_math import CS_BIN
from tests.test_sundae_v4_banded_cl_math import CS_BIN_STATE
from tests.test_sundae_v4_banded_cl_math import DROP_LADDER
from tests.test_sundae_v4_banded_cl_math import DROP_STATE
from tests.test_sundae_v4_banded_cl_math import P1_CLOSING
from tests.test_sundae_v4_banded_cl_math import P1_QUOTES
from tests.test_sundae_v4_banded_cl_math import P1_RESERVES
from tests.test_sundae_v4_banded_cl_math import P1_STARTS
from tests.test_sundae_v4_banded_cl_math import SLIVER_LADDER
from tests.test_sundae_v4_banded_cl_math import SLIVER_STATE
from tests.test_sundae_v4_banded_cl_math import p1_ladder
from tests.test_sundae_v4_banded_cl_pool import P1_EDGE

CASES = random_cases(seed=20261008, count=150)
SIZES = (1e-6, 0.01, 0.3, 0.9, 1.5, 4.0)
LP = 10**12


def _offers(state: tuple[int, int], a_in: bool) -> list[int]:
    cap = state[1] if a_in else state[0]
    return sorted({max(1, int(cap * frac)) for frac in SIZES})


def _moved(
    a: int, b: int, a_in: bool, amount_in: int, amount_out: int
) -> tuple[int, int]:
    return (a + amount_in, b - amount_out) if a_in else (a - amount_out, b + amount_in)


def _transcript(
    shape: dict, state: tuple[int, int], quote: BandedQuote, a_in: bool
) -> list[oracle.Entry] | None:
    """The transcript the quote declares, or ``None`` if its end cannot be bounded.

    Each step's after-pair is the next step's witness; the last boundary declares
    a pair the oracle finds on its own.
    """
    bands, closing, wt = oracle_bands(shape), shape["closing"], shape["weight_total"]
    entries = []
    a, b = state
    for i, step in enumerate(quote.steps):
        a, b = _moved(a, b, a_in, step.amount_in, step.amount_out)
        if i + 1 < len(quote.steps):
            nxt = quote.steps[i + 1].witness
            pair: tuple[int, int] | None = (nxt.counter, nxt.band)
        else:
            pair = oracle.bounded_pair((a, b), step.witness.counter, bands, closing, wt)
            if pair is None:
                return None
        fee_budget = oracle.swap_fee_budget(step.witness.counter, LP, pair[0], LP)
        entries.append(oracle.Entry((a, b), pair[0], pair[1], LP, fee_budget))
    return entries


def _accepted(
    shape: dict,
    state: tuple[int, int],
    start: Witness,
    entries: list[oracle.Entry],
) -> bool:
    return oracle.banded_walk_check(
        entries,
        state,
        start.counter,
        start.band,
        LP,
        oracle_bands(shape),
        shape["closing"],
        shape["weight_total"],
    )


# ── every quote, as a transcript ─────────────────────────────────────────────


def test_every_quote_is_a_transcript_the_validator_accepts() -> None:
    seen: Counter = Counter()
    for shape, ladder, state in CASES:
        assert find_witness(ladder, *state) is not None, (shape, state)
        bands = oracle_bands(shape)
        for a_in in (True, False):
            for dx in _offers(state, a_in):
                quote = banded_quote(ladder, *state, dx, a_in)
                seen["quotes"] += 1
                assert 0 <= quote.spent <= dx
                if not quote.steps:
                    seen["nothing bought"] += 1
                    continue
                entries = _transcript(shape, state, quote, a_in)
                assert entries is not None, (shape, state, a_in, dx)
                start = quote.steps[0].witness
                assert _accepted(shape, state, start, entries), (shape, state, a_in, dx)
                # One more unit out of any step fails, with the same declared pairs.
                before, x, k = state, start.counter, start.band
                for entry in entries:
                    a1, b1 = entry.assets
                    greedy = (a1, b1 - 1) if a_in else (a1 - 1, b1)
                    assert not oracle.banded_step(
                        before,
                        x,
                        k,
                        LP,
                        greedy,
                        entry.x,
                        LP,
                        entry.fee_budget,
                        bands,
                        shape["closing"],
                        shape["weight_total"],
                    )
                    before, x, k = entry.assets, entry.x, entry.k
                # The part the ladder absorbs is an offer it absorbs in full.
                if quote.spent < dx:
                    seen["stopped early"] += 1
                    again = banded_quote(ladder, *state, quote.spent, a_in)
                    assert (again.amount_out, again.spent) == (
                        quote.amount_out,
                        quote.spent,
                    )
                seen["steps"] += len(quote.steps)
                seen["crossings"] += len(quote.steps) - 1
                seen["constant-sum steps"] += sum(
                    ladder.bands[s.witness.band].curve == 1 for s in quote.steps
                )
                seen["sells A" if a_in else "buys A"] += 1
    assert seen["quotes"] >= 1_500
    assert seen["crossings"] >= 400
    assert seen["constant-sum steps"] >= 400
    assert seen["stopped early"] >= 300
    assert min(seen["sells A"], seen["buys A"]) >= 500


def test_the_p1_quotes_are_transcripts_the_validator_accepts() -> None:
    n = len(P1_STARTS)
    shape = {
        "starts": P1_STARTS,
        "closing": P1_CLOSING,
        "weights": [1] * n,
        "curves": [0] * n,
        "fees_buy": [(3, 1000)] * n,
        "fees_sell": [(3, 1000)] * n,
        "weight_total": n,
    }
    ladder = Ladder.from_shape(**shape)
    for dx, a_in, want, _bands in P1_QUOTES:
        quote = banded_quote(ladder, *P1_RESERVES, dx, a_in)
        assert (quote.amount_out, quote.spent) == (want, dx)
        entries = _transcript(shape, P1_RESERVES, quote, a_in)
        assert entries is not None
        assert _accepted(shape, P1_RESERVES, quote.steps[0].witness, entries)


def test_a_quote_resumed_from_the_drain_is_the_quote_walked_afresh() -> None:
    named = [
        (Ladder.from_shape(**shape), state)
        for shape, state in (
            (DROP_LADDER, DROP_STATE),
            (CS_BIN, CS_BIN_STATE),
            (SLIVER_LADDER, SLIVER_STATE),
        )
    ]
    named += [(p1_ladder(), P1_RESERVES), (p1_ladder(), P1_EDGE)]
    for ladder, state in [(ladder, state) for _, ladder, state in CASES[:60]] + named:
        for a_in in (True, False):
            start = directed_witness(ladder, *state, a_in)
            drain = banded_quote(ladder, *state, 2**64 - 1, a_in, start)
            sizes = set(_offers(state, a_in)) | {drain.spent, drain.spent + 1}
            cum = 0
            for step in drain.steps:
                sizes |= {cum + step.amount_in + d for d in (-1, 0, 1)}
                cum += step.amount_in
            for dx in sorted(size for size in sizes if size > 0):
                walked = banded_quote(ladder, *state, dx, a_in, start)
                assert banded_quote(ladder, *state, dx, a_in, start, drain) == walked


def test_witnesses_are_tight() -> None:
    for _shape, ladder, state in CASES:
        witness = find_witness(ladder, *state)
        assert witness is not None
        assert is_witness(ladder, *state, witness.counter, witness.band)
        assert not achievable(ladder, *state, witness.counter + 1, witness.band)
        assert not is_witness(ladder, *state, witness.counter - 1, witness.band)


def test_liquidity_moves_pass_the_proportional_check_and_are_extremal() -> None:
    checked = 0
    for shape, ladder, state in CASES:
        a, b = state
        if min(a, b) < 100:
            continue
        witness = find_witness(ladder, a, b)
        assert witness is not None
        lp = witness.counter
        bands, closing, wt = (
            oracle_bands(shape),
            shape["closing"],
            shape["weight_total"],
        )

        def step_ok(
            after: tuple[int, int], lp_after: int, x: int = witness.counter
        ) -> bool:
            return oracle.banded_step(
                (a, b), x, witness.band, lp, after, 0, lp_after, 0, bands, closing, wt
            )

        deposit = banded_cl_pinned_deposit([a, b], [a // 7, b // 7], lp)
        after = (a + deposit.deltas[0], b + deposit.deltas[1])
        assert step_ok(after, deposit.lp_after)
        assert not step_ok((after[0] - 1, after[1]), deposit.lp_after)
        assert not step_ok((after[0], after[1] - 1), deposit.lp_after)
        assert not step_ok(after, deposit.lp_after + 1)
        withdraw = banded_cl_pinned_withdraw([a, b], lp // 3, lp)
        paid = (a - withdraw.payouts[0], b - withdraw.payouts[1])
        assert step_ok(paid, withdraw.lp_after)
        assert not step_ok((paid[0] - 1, paid[1]), withdraw.lp_after)
        assert not step_ok((paid[0], paid[1] - 1), withdraw.lp_after)
        checked += 1
    assert checked >= 100


# ── the pool type on the same cases ─────────────────────────────────────────


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
def test_the_pool_type_quotes_whole_offers_and_inverts_them() -> None:
    a_unit, b_unit = "01" * 28 + "0a", "02" * 28 + "0b"
    module = SundaeV4Deployment.for_network("preview").banded_cl_hash
    for shape, ladder, state in CASES[:50]:
        values, config = build_banded_cl_vault_utxo(
            [(a_unit, state[0]), (b_unit, state[1])],
            starts=shape["starts"],
            closing=shape["closing"],
            weights=shape["weights"],
            curves=shape["curves"],
            fee_buy=shape["fees_buy"],
            fee_sell=shape["fees_sell"],
            total_lp=LP,
        )
        vault = SundaeV4Vault.model_validate(values)
        vault.supply_module_config(module, config)
        (pool,) = vault.pools()
        assert isinstance(pool, SundaeV4BandedCLPool)
        for a_in in (True, False):
            unit_in, unit_out = (a_unit, b_unit) if a_in else (b_unit, a_unit)
            top = pool.max_output(unit_out) - 1
            for dx in _offers(state, a_in):
                quote = banded_quote(ladder, *state, dx, a_in)
                out = pool.get_amount_out(Assets(**{unit_in: dx}), unit_out)[0]
                full = quote.spent == dx
                assert out.quantity() == (quote.amount_out if full else 0)
                assert quote.amount_out <= top
                if quote.amount_out > 0:
                    want = Assets(**{unit_out: quote.amount_out})
                    needed = pool.get_amount_in(want, unit_in)[0].quantity()
                    assert needed <= quote.spent
                    paid = pool.get_amount_out(Assets(**{unit_in: needed}), unit_out)
                    assert paid[0].quantity() >= quote.amount_out
            if top > 0:
                needed = pool.get_amount_in(Assets(**{unit_out: top}), unit_in)[0]
                assert pool.get_amount_out(needed, unit_out)[0].quantity() == top


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
    for shape, ladder, _state in CASES[:20]:
        bands = oracle_bands(shape)
        closing, wt = shape["closing"], shape["weight_total"]
        idx = oracle.build_index(bands, closing, wt)
        assert idx == list(ladder.index)
        for x in (ladder.weight_total, 10**6 + 3, 10**12 + 11):
            for k in range(ladder.n):
                st: BandState = ladder.at(x, k)
                ost = oracle.ladder_at_upper(bands, idx, closing, wt, x, k)
                assert (st.ca, st.cb, st.a_sat, st.b_sat, st.liquidity) == (
                    ost.ca,
                    ost.cb,
                    ost.a_sat_k,
                    ost.b_sat_k,
                    ost.l_k,
                )


def test_a_last_step_may_end_bounded_in_a_band_far_from_its_own() -> None:
    # A 50% sell fee in the top band: one step there leaves more A in the pool
    # than band 8 holds at any counter the walk may end on, but the state is
    # bounded in band 2 at a far higher counter, so the whole offer executes.
    den = 9223372036854775808
    shape = {
        "starts": [
            (30932469293718972488, den),
            (32612286819563209311, den),
            (37787725851959984833, den),
            (43926395007499411313, den),
            (47814864482958151664, den),
            (55936378533002115319, den),
            (59889841501956611787, den),
            (59905877031671215597, den),
            (59917467184599434007, den),
        ],
        "closing": (63455918188860887148, den),
        "weights": [1000000, 1000, 1000000, 1, 1, 1000, 3, 1, 1000000],
        "curves": [1, 1, 1, 1, 0, 1, 1, 0, 0],
        "fees_buy": [(3, 1000), (99, 100), (1, 2 * den), (1, 2 * den), (3, 1000)]
        + [(99, 100)] * 4,
        "fees_sell": [(25, 10000), (1, 10), (1, 10), (0, 1), (25, 10000)]
        + [(den, 2 * den), (1, 10), (den, 2 * den), (den, 2 * den)],
        "weight_total": 3002006,
    }
    ladder = Ladder.from_shape(**shape)
    state = (107_748_310, 93_364_091_608)
    quote = banded_quote(ladder, *state, 933_640_916, True)
    assert quote.spent == 933_640_916
    assert [(s.witness.band, s.amount_out) for s in quote.steps] == [
        (8, 20_860_330_112)
    ]
    entries = _transcript(shape, state, quote, True)
    assert entries is not None
    assert (entries[-1].k, entries[-1].x) == (2, 274_283_275_824)
    assert _accepted(shape, state, quote.steps[0].witness, entries)
