"""Generate randomised SundaeSwap V4 banded-CL vectors and their on-chain check.

Builds random ladders (1-9 bands, mixed CL arcs and constant-sum bins, uneven
weights, asymmetric fees), random priceable states, and quotes them with the
dendrite math. Every swap step and liquidity move is then checked with the
Python port of the validator (``tests/sundae_v4_bcl_oracle.py``) and written to

* a JSON fixture, which ``tests/test_sundae_v4_banded_cl_oracle.py`` replays;
* an Aiken test module calling ``banded_cl_check.banded_step_ix`` — the code the
  chain runs — once per step (and once more, expecting failure, with one extra
  unit of output). Drop it into ``lib/tests/unit/`` of the sundae-v4 contracts
  repository and run ``aiken check -m "banded_vectors_gen.{..}"``.

Usage: python scripts/gen_sundae_v4_banded_cl_vectors.py <fixture.json> <aiken.ak> [seed]
Run from the repository root.
"""

# ruff: noqa
# A one-off generator: prints, random, and long literal tables are the point.

from __future__ import annotations

import json
import random
import sys

sys.path.insert(0, ".")

from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import (  # noqa: E402
    Ladder,
    banded_quote,
    curve_g,
    find_witness,
)
from tests.sundae_v4_bcl_oracle import banded_step, swap_fee_budget  # noqa: E402

COUNTER_DROPS: list = []


def rand_ladder(rng: random.Random) -> dict:
    n = rng.choice([1, 1, 2, 3, 4, 5, 8, 9])
    den = rng.choice([1, 10, 1000, 80000, 10**6])
    # Strictly increasing numerators; occasionally wide, occasionally tight.
    base = rng.randint(1, 5 * den)
    nums = [base]
    for _ in range(n):
        nums.append(
            nums[-1] + rng.randint(1, max(1, den // rng.choice([1, 4, 40, 400])))
        )
    starts = [(p, den) for p in nums[:-1]]
    closing = (nums[-1], den)
    weights = [rng.choice([1, 1, 2, 3, 7, 100, 199]) for _ in range(n)]
    curves = [rng.choice([0, 0, 1]) for _ in range(n)]
    fees_buy = [
        rng.choice([(0, 1), (3, 1000), (1, 100), (5, 10000), (7, 997)])
        for _ in range(n)
    ]
    fees_sell = [
        rng.choice([(0, 1), (3, 1000), (25, 10000), (1, 10), (11, 1009)])
        for _ in range(n)
    ]
    return dict(
        starts=starts,
        closing=closing,
        weights=weights,
        curves=curves,
        fees_buy=fees_buy,
        fees_sell=fees_sell,
        weight_total=sum(weights),
    )


def ladder_of(shape: dict) -> Ladder:
    return Ladder.from_shape(**shape)


def rand_state(rng: random.Random, lad: Ladder) -> tuple[int, int] | None:
    """Reserves inside a random band at a random counter, built from the band's own curve."""
    k = rng.randrange(lad.n)
    x = rng.randint(
        lad.weight_total, rng.choice([10**6, 10**9, 10**12, 10**15])
    )
    st = lad.at(x, k)
    band = lad.bands[k]
    if st.a_sat <= 0 or st.b_sat <= 0:
        return None
    ra = rng.randint(0, st.a_sat)
    # smallest rb with G >= 0
    lo, hi = 0, st.b_sat + 1
    if curve_g(band, ra, hi, st.liquidity) < 0:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if curve_g(band, ra, mid, st.liquidity) >= 0:
            hi = mid
        else:
            lo = mid + 1
    rb = lo
    if rb > st.b_sat:
        return None
    return (ra + st.ca, rb + st.cb)


def oracle_bands(shape: dict) -> list[dict]:
    return [
        dict(start=s, weight=w, curve=c, fee_buy=fb, fee_sell=fs)
        for s, w, c, fb, fs in zip(
            shape["starts"],
            shape["weights"],
            shape["curves"],
            shape["fees_buy"],
            shape["fees_sell"],
        )
    ]


def main() -> None:
    fixture_path, aiken_path = sys.argv[1], sys.argv[2]
    rng = random.Random(int(sys.argv[3]) if len(sys.argv) > 3 else 20261008)
    ladders = []
    swaps = []
    liquidity = []
    rejected = {
        "unpriceable_state": 0,
        "x_after_below_x_before": 0,
        "oracle_fail": 0,
        "zero_output_step": 0,
    }
    tries = 0
    while len(swaps) < 260 and tries < 5000:
        tries += 1
        shape = rand_ladder(rng)
        lad = ladder_of(shape)
        state = rand_state(rng, lad)
        if state is None:
            continue
        a, b = state
        w = find_witness(lad, a, b)
        if w is None:
            rejected["unpriceable_state"] += 1
            continue
        lid = len(ladders)
        ladders.append(dict(shape, index=[list(e) for e in lad.index]))
        obands = oracle_bands(shape)
        lp = rng.randint(w.counter, 3 * w.counter)
        # swaps: a few sizes in both directions
        for a_in in (True, False):
            cap = b if a_in else a
            if cap <= 0:
                continue
            for frac in (
                rng.uniform(0, 0.02),
                rng.uniform(0.02, 0.5),
                rng.uniform(0.5, 1.2),
                3.0,
            ):
                dx = max(1, int(cap * frac))
                q = banded_quote(lad, a, b, dx, a_in)
                if not q.steps:
                    continue
                steps = []
                ca, cb = a, b
                ok_all = True
                for i, step in enumerate(q.steps):
                    wit = step.witness
                    na, nb = (
                        (ca + step.amount_in, cb - step.amount_out)
                        if a_in
                        else (ca - step.amount_out, cb + step.amount_in)
                    )
                    if step.amount_out == 0:
                        # Not a swap on chain (b1 == b0): a donation. Nothing to price.
                        rejected["zero_output_step"] += 1
                        ok_all = False
                        break
                    crossing = i + 1 < len(q.steps)
                    nxt = wit.band - 1 if a_in else wit.band + 1
                    # The chain accepts any valid after-witness whose counter did not
                    # fall (fee_budget >= 0). At an edge both bands admit one; take the
                    # one that keeps the counter, preferring the band being entered.
                    cands = [
                        find_witness(lad, na, nb, kk)
                        for kk in ((nxt, wit.band) if crossing else (wit.band, nxt))
                        if 0 <= kk < lad.n
                    ]
                    cands = [c for c in cands if c is not None]
                    keep = [c for c in cands if c.counter >= wit.counter]
                    w_after = keep[0] if keep else (cands[0] if cands else None)
                    if w_after is None:
                        ok_all = False
                        break
                    if w_after.counter < wit.counter:
                        rejected["x_after_below_x_before"] += 1
                        COUNTER_DROPS.append(
                            dict(
                                ladder=lid,
                                before=[ca, cb],
                                x=wit.counter,
                                k=wit.band,
                                after=[na, nb],
                                x_after=w_after.counter,
                                k_after=w_after.band,
                                a_is_input=a_in,
                                amount_in=step.amount_in,
                                amount_out=step.amount_out,
                            )
                        )
                        ok_all = False
                        break
                    fb = swap_fee_budget(wit.counter, lp, w_after.counter, lp)
                    ok = banded_step(
                        (ca, cb),
                        wit.counter,
                        wit.band,
                        lp,
                        (na, nb),
                        w_after.counter,
                        lp,
                        fb,
                        obands,
                        shape["closing"],
                        shape["weight_total"],
                    )
                    if not ok:
                        rejected["oracle_fail"] += 1
                        ok_all = False
                        break
                    steps.append(
                        dict(
                            before=[ca, cb],
                            x=wit.counter,
                            k=wit.band,
                            after=[na, nb],
                            x_after=w_after.counter,
                            k_after=w_after.band,
                            fee_budget=fb,
                            amount_in=step.amount_in,
                            amount_out=step.amount_out,
                        )
                    )
                    ca, cb = na, nb
                if not ok_all:
                    continue
                swaps.append(
                    dict(
                        ladder=lid,
                        reserves=[a, b],
                        witness=[w.counter, w.band],
                        lp=lp,
                        a_is_input=a_in,
                        dx=dx,
                        amount_out=q.amount_out,
                        spent=q.spent,
                        bands=list(q.bands),
                        steps=steps,
                    )
                )
        # liquidity: a deposit and a withdrawal via the proportional check
        offered = [max(1, a // rng.randint(2, 50)), max(1, b // rng.randint(2, 50))]
        if a > 0 and b > 0:
            target = min(o * lp // r for o, r in zip(offered, (a, b)))
            if target > 0:
                deltas = [-(-(r * target) // lp) for r in (a, b)]
                after = [a + deltas[0], b + deltas[1]]
                dep_ok = banded_step(
                    (a, b),
                    w.counter,
                    w.band,
                    lp,
                    tuple(after),
                    0,
                    lp + target,
                    0,
                    obands,
                    shape["closing"],
                    shape["weight_total"],
                )
                burn = rng.randint(1, lp)
                payouts = [r * burn // lp for r in (a, b)]
                after_w = [a - payouts[0], b - payouts[1]]
                wd_ok = banded_step(
                    (a, b),
                    w.counter,
                    w.band,
                    lp,
                    tuple(after_w),
                    0,
                    lp - burn,
                    0,
                    obands,
                    shape["closing"],
                    shape["weight_total"],
                )
                liquidity.append(
                    dict(
                        ladder=lid,
                        reserves=[a, b],
                        witness=[w.counter, w.band],
                        lp=lp,
                        offered=offered,
                        deposit_after=after,
                        lp_after=lp + target,
                        deposit_ok=dep_ok,
                        burn=burn,
                        withdraw_after=after_w,
                        withdraw_ok=wd_ok,
                    )
                )
    fixture = dict(
        _comment="Randomised banded CL vectors generated by scripts/gen_sundae_v4_banded_cl_vectors.py "
        "with the dendrite math and checked by the on-chain banded_cl_check.banded_step (aiken, "
        "sundae v4-pool-perf b620a7e) and its Python port tests/sundae_v4_bcl_oracle.py. Every "
        "swap step passes the check and one more unit out fails it; every deposit/withdrawal "
        "passes and one unit short/greedy fails. counter_drops are edge crossings out of a "
        "tiny-weight bin where the entering band's witness re-derives a lower counter: valid "
        "pricing, but not executable as one transcript (the budget pair needs fee_budget >= 0).",
        seed=int(sys.argv[3]) if len(sys.argv) > 3 else 20261008,
        ladders=ladders,
        swaps=swaps,
        liquidity=liquidity,
        rejected=rejected,
        counter_drops=COUNTER_DROPS,
    )
    with open(fixture_path, "w") as f:
        json.dump(fixture, f, indent=1)

    # --- Aiken module -------------------------------------------------------
    def r(x):
        return f"Rational {{ num: {x[0]}, den: {x[1]} }}"

    out = [
        "// GENERATED by dendrite gen_vectors.py — do not edit.",
        "use modules/banded_cl_check.{banded_step_ix}",
        "use tests/builders.{asset_a, asset_b}",
        "use types/banded_cl.{BandSpec}",
        "use types/common.{AssetClass, Rational}",
        "",
        "fn reserves(a: Int, b: Int) -> List<(AssetClass, Int)> {",
        "  [(asset_a, a), (asset_b, b)]",
        "}",
        "",
    ]
    for i, lad in enumerate(ladders):
        bands = ",\n".join(
            f"    BandSpec {{ start: {r(s)}, weight: {w}, curve: {c}, fee_buy: {r(fb)}, fee_sell: {r(fs)} }}"
            for s, w, c, fb, fs in zip(
                lad["starts"],
                lad["weights"],
                lad["curves"],
                lad["fees_buy"],
                lad["fees_sell"],
            )
        )
        out += [f"fn l{i}() -> List<BandSpec> {{", "  [", bands, "  ]", "}", ""]

    def call(lid, lad, st, after, lp_after, pass_):
        a0, b0 = st["before"]
        a1, b1 = after
        return (
            f"  banded_step_ix(reserves({a0}, {b0}), {st['x']}, {st['k']}, {st['lp']}, "
            f"reserves({a1}, {b1}), {st['x_after']}, {lp_after}, {st['fee_budget']}, "
            f"l{lid}(), {r(lad['closing'])}, {lad['weight_total']})"
        )

    n_tests = 0
    for si, sw in enumerate(swaps):
        lad = ladders[sw["ladder"]]
        for ti, st in enumerate(sw["steps"]):
            st = dict(st, lp=sw["lp"])
            out += [
                f"test v_swap_{si}_{ti}() {{",
                call(sw["ladder"], lad, st, st["after"], sw["lp"], True),
                "}",
                "",
            ]
            # one more unit out
            a1, b1 = st["after"]
            greedy = [a1, b1 - 1] if sw["a_is_input"] else [a1 - 1, b1]
            out += [
                f"test v_swap_{si}_{ti}_greedy() fail {{",
                call(sw["ladder"], lad, st, greedy, sw["lp"], False),
                "}",
                "",
            ]
            n_tests += 2
    for li, lq in enumerate(liquidity):
        lad = ladders[lq["ladder"]]
        st = dict(
            before=lq["reserves"],
            x=lq["witness"][0],
            k=lq["witness"][1],
            lp=lq["lp"],
            x_after=0,
            fee_budget=0,
        )
        out += [
            f"test v_dep_{li}() {{",
            call(lq["ladder"], lad, st, lq["deposit_after"], lq["lp_after"], True),
            "}",
            "",
        ]
        a1, b1 = lq["deposit_after"]
        out += [
            f"test v_dep_{li}_short() fail {{",
            call(lq["ladder"], lad, st, [a1 - 1, b1], lq["lp_after"], False),
            "}",
            "",
        ]
        out += [
            f"test v_wd_{li}() {{",
            call(
                lq["ladder"], lad, st, lq["withdraw_after"], lq["lp"] - lq["burn"], True
            ),
            "}",
            "",
        ]
        a1, b1 = lq["withdraw_after"]
        out += [
            f"test v_wd_{li}_greedy() fail {{",
            call(lq["ladder"], lad, st, [a1, b1 - 1], lq["lp"] - lq["burn"], False),
            "}",
            "",
        ]
        n_tests += 4
    with open(aiken_path, "w") as f:
        f.write("\n".join(out) + "\n")
    print(
        f"ladders={len(ladders)} swaps={len(swaps)} steps={sum(len(s['steps']) for s in swaps)} "
        f"liquidity={len(liquidity)} aiken_tests={n_tests} rejected={rejected} tries={tries}"
    )
    cs_steps = sum(
        1
        for s in swaps
        for st in s["steps"]
        if ladders[s["ladder"]]["curves"][st["k"]] == 1
    )
    multi = sum(1 for s in swaps if len(s["steps"]) > 1)
    print(
        f"cs_steps={cs_steps} multi_band_swaps={multi} "
        f"uneven={sum(1 for l in ladders if len(set(l['weights']))>1)} "
        f"asym_fee={sum(1 for l in ladders if l['fees_buy']!=l['fees_sell'])}"
    )


if __name__ == "__main__":
    main()
