"""Python port of the banded CL validator checks (``banded_cl_check.ak``).

Every predicate is integer-exact and mirrors the Aiken source clause for clause:
``ladder_at`` / ``ladder_at_upper``, ``band_proof`` (P2-P6), ``band_proof_tight``
(P7), ``banded_swap`` with ``cl_check.swap_output_pair`` and ``cs_output_pair``,
``cl_check.budget_pair_form3``, ``cl_check.check_proportional``, ``banded_step``,
``final_boundary_bounded`` and the transcript walk ``banded_walk_check_raw``.
These are the *checks* the chain runs on a declared transcript, not the quote
arithmetic, so they are an independent test of the quote: a quote is right when
the transcript it declares passes and one more unit out fails.

The prefix sums ``C_A`` / ``C_B`` follow the deployed module
(``banded_concentrated_liquidity.withdraw`` ``33485bba...`` on preview): each is
read from the config's ladder index, ``ceil(index[k] * X / 2**64)``, where the
published pre-index source sums one rounded-up saturation constant per band. The
two rules differ by rounding, and recorded preview scoops pass only the index
rule.

Ladders are plain Python: ``bands`` is a list of dicts with ``start``, ``weight``,
``curve``, ``fee_buy``, ``fee_sell`` (rationals as ``(num, den)`` tuples),
``closing`` a rational, ``wt`` the weight total, ``index`` the list of
``(ca_coeff, cb_coeff)`` pairs.
"""

from __future__ import annotations

from dataclasses import dataclass

FIXED_ONE = 1 << 64


def ceil_div(n: int, d: int) -> int:
    return (n + d - 1) // d


def build_index(
    bands: list[dict], closing: tuple[int, int], wt: int
) -> list[tuple[int, int]]:
    """``banded_cl_check.build_index``: ``index_from(coeffs(...), 0)``."""
    edges = [b["start"] for b in bands] + [closing]
    coeffs = []
    for i, b in enumerate(bands):
        (p0, q0), (p1, q1) = edges[i], edges[i + 1]
        d = p1 * q0 - p0 * q1
        coeffs.append(
            (
                ceil_div(b["weight"] * d * FIXED_ONE, wt * p0 * p1),
                ceil_div(b["weight"] * d * FIXED_ONE, wt * q0 * q1),
            )
        )
    out: list[tuple[int, int]] = []
    cb_acc = 0
    for i, (ca, cb) in enumerate(coeffs):
        suffix_a = sum(c[0] for c in coeffs[i + 1 :])
        out.append((suffix_a, cb_acc))
        cb_acc += cb
    return out


@dataclass(frozen=True)
class LadderState:
    ca: int
    cb: int
    a_sat_k: int
    b_sat_k: int
    l_k: int
    lo: tuple[int, int]
    hi: tuple[int, int]
    band: dict


@dataclass(frozen=True)
class LadderTight:
    ca_next: int
    cb_next: int
    l_k_next: int


def ladder_at_upper(
    bands: list[dict], index: list, closing: tuple[int, int], wt: int, x: int, k: int
) -> LadderState:
    band = bands[k]
    lo = band["start"]
    hi = bands[k + 1]["start"] if k + 1 < len(bands) else closing
    ca_coeff, cb_coeff = index[k]
    num = band["weight"] * (hi[0] * lo[1] - lo[0] * hi[1])
    return LadderState(
        ca=ceil_div(ca_coeff * x, FIXED_ONE),
        cb=ceil_div(cb_coeff * x, FIXED_ONE),
        a_sat_k=ceil_div(num * x, wt * lo[0] * hi[0]),
        b_sat_k=ceil_div(num * x, wt * lo[1] * hi[1]),
        l_k=band["weight"] * x // wt,
        lo=lo,
        hi=hi,
        band=band,
    )


def ladder_at(
    bands: list[dict], index: list, closing: tuple[int, int], wt: int, x: int, k: int
) -> tuple[LadderState, LadderTight]:
    st = ladder_at_upper(bands, index, closing, wt, x, k)
    ca_coeff, cb_coeff = index[k]
    return st, LadderTight(
        ca_next=ceil_div(ca_coeff * (x + 1), FIXED_ONE),
        cb_next=ceil_div(cb_coeff * (x + 1), FIXED_ONE),
        l_k_next=bands[k]["weight"] * (x + 1) // wt,
    )


def f_at_raw(
    a: int, b: int, l: int, spa_num: int, spa_den: int, spb_num: int, spb_den: int
) -> int:
    return (a * spb_num + l * spb_den) * (
        b * spa_den + l * spa_num
    ) - l * l * spb_num * spa_den


def g_of(st: LadderState, ra: int, rb: int, l: int) -> int:
    if st.band["curve"] == 0:
        return f_at_raw(ra, rb, l, st.lo[0], st.lo[1], st.hi[0], st.hi[1])
    return (
        ra * (st.lo[0] * st.hi[0])
        + rb * (st.lo[1] * st.hi[1])
        - l * (st.hi[0] * st.lo[1] - st.lo[0] * st.hi[1])
    )


def band_proof(a: int, b: int, st: LadderState) -> bool:
    """P2-P6."""
    ra, rb = a - st.ca, b - st.cb
    return (
        ra >= 0
        and rb >= 0
        and ra <= st.a_sat_k
        and rb <= st.b_sat_k
        and g_of(st, ra, rb, st.l_k) >= 0
    )


def band_proof_tight(a: int, b: int, st: LadderState, tg: LadderTight) -> bool:
    """P2-P7."""
    return (
        band_proof(a, b, st)
        and g_of(st, a - tg.ca_next, b - tg.cb_next, tg.l_k_next) < 0
    )


def swap_output_pair(
    a0: int,
    b0: int,
    a1: int,
    b1: int,
    va0: int,
    vb0: int,
    dx_eff: int,
    spa_den: int,
    spb_num: int,
) -> bool:
    """``cl_check.swap_output_pair``: achievable and tight on the virtual reserves."""
    if a1 > a0:
        dva_eff = dx_eff * spb_num
        dvb = (b0 - b1) * spa_den
        return (
            dvb * (va0 + dva_eff) <= vb0 * dva_eff
            and (dvb + spa_den) * (va0 + dva_eff) > vb0 * dva_eff
        )
    dvb_eff = dx_eff * spa_den
    dva = (a0 - a1) * spb_num
    return (
        dva * (vb0 + dvb_eff) <= va0 * dvb_eff
        and (dva + spb_num) * (vb0 + dvb_eff) > va0 * dvb_eff
    )


def cs_output_pair(
    ra0: int,
    rb0: int,
    ra1: int,
    rb1: int,
    a_is_input: bool,
    dx_eff: int,
    lo: tuple,
    hi: tuple,
) -> bool:
    pn, pd = lo[0] * hi[0], lo[1] * hi[1]
    if a_is_input:
        out = rb0 - rb1
        return out * pd <= dx_eff * pn and (out + 1) * pd > dx_eff * pn
    out = ra0 - ra1
    return out * pn <= dx_eff * pd and (out + 1) * pn > dx_eff * pd


def budget_pair_form3(cl_before: int, lp_before: int, cl_after: int, bp: int) -> bool:
    rhs = lp_before * cl_after
    return bp * cl_before <= rhs and (bp + 1) * cl_before > rhs


def banded_swap(
    a0: int,
    b0: int,
    a1: int,
    b1: int,
    st: LadderState,
    x_before: int,
    lp_before: int,
    x_after: int,
    lp_after: int,
    fee_budget: int,
) -> bool:
    """``banded_cl_check.banded_swap``; ``expect`` clauses become ``False``."""
    if fee_budget < 0:
        return False
    ra0, rb0, ra1, rb1 = a0 - st.ca, b0 - st.cb, a1 - st.ca, b1 - st.cb
    if ra1 < 0 or rb1 < 0:
        return False
    fee_r = st.band["fee_buy"] if a1 < a0 else st.band["fee_sell"]
    dx = a1 - a0 if a1 > a0 else b1 - b0
    fee = dx * fee_r[0] // fee_r[1]
    dx_eff = dx - fee
    if st.band["curve"] == 0:
        va0 = ra0 * st.hi[0] + st.l_k * st.hi[1]
        vb0 = rb0 * st.lo[1] + st.l_k * st.lo[0]
        ok = swap_output_pair(a0, b0, a1, b1, va0, vb0, dx_eff, st.lo[1], st.hi[0])
    else:
        ok = cs_output_pair(ra0, rb0, ra1, rb1, a1 > a0, dx_eff, st.lo, st.hi)
    return ok and budget_pair_form3(x_before, lp_before, x_after, lp_after + fee_budget)


def check_proportional(
    before: list[int], after: list[int], lp_before: int, lp_after: int
) -> bool:
    """``cl_check.check_proportional``."""
    if not (lp_before > 0 and lp_after >= 0):
        return False
    for amt_before, amt_after in zip(before, after):
        if amt_after < 0 or amt_after * lp_before < amt_before * lp_after:
            return False
    return True


def banded_step(
    before: tuple[int, int],
    x_before: int,
    k_before: int,
    lp_before: int,
    after: tuple[int, int],
    x_after: int,
    lp_after: int,
    fee_budget: int,
    bands: list[dict],
    closing: tuple[int, int],
    wt: int,
    index: list | None = None,
) -> bool:
    """``banded_cl_check.banded_step``: one swap or non-swap step, as the chain checks it."""
    index = index if index is not None else build_index(bands, closing, wt)
    (a0, b0), (a1, b1) = before, after
    is_swap = (a1 > a0 and b1 < b0) or (a1 < a0 and b1 > b0)
    if x_before < wt:
        return False
    if is_swap:
        st, tg = ladder_at(bands, index, closing, wt, x_before, k_before)
        return band_proof_tight(a0, b0, st, tg) and banded_swap(
            a0, b0, a1, b1, st, x_before, lp_before, x_after, lp_after, fee_budget
        )
    st = ladder_at_upper(bands, index, closing, wt, x_before, k_before)
    return (
        band_proof(a0, b0, st)
        and check_proportional([a0, b0], [a1, b1], lp_before, lp_after)
        and fee_budget == 0
    )


def swap_fee_budget(x_before: int, lp_before: int, x_after: int, lp_after: int) -> int:
    """The one ``fee_budget`` the budget pair admits: ``floor(lp_before * x_after / x_before) - lp_after``."""
    return lp_before * x_after // x_before - lp_after


def final_boundary_bounded(
    after: tuple[int, int],
    x: int,
    k: int,
    bands: list[dict],
    closing: tuple[int, int],
    wt: int,
    index: list | None = None,
) -> bool:
    """``banded_cl_check.final_boundary_bounded``: P1 and P2-P6 on the last boundary."""
    index = index if index is not None else build_index(bands, closing, wt)
    if x < wt:
        return False
    return band_proof(*after, ladder_at_upper(bands, index, closing, wt, x, k))


@dataclass(frozen=True)
class Entry:
    """One transcript entry: the state after a step and the pair declared for it.

    ``assets`` is ``(a, b)``; ``x`` / ``k`` the declared counter and band; ``lp``
    the total LP after the step; ``fee_budget`` the step's declared budget.
    """

    assets: tuple[int, int]
    x: int
    k: int
    lp: int
    fee_budget: int


def banded_walk_check(
    transcript: list[Entry],
    before: tuple[int, int],
    x_before: int,
    k_before: int,
    lp_before: int,
    bands: list[dict],
    closing: tuple[int, int],
    wt: int,
    index: list | None = None,
) -> bool:
    """``banded_cl_check.banded_walk_check_raw``: thread the pair, bound the end.

    Every step runs ``banded_step`` from the previous boundary's pair; a swap's
    budget pair needs ``fee_budget >= 0``, so the counter never falls within one
    transcript while the LP holds. The last boundary owes P1-P6 only.
    """
    index = index if index is not None else build_index(bands, closing, wt)
    assets, x, k, lp = before, x_before, k_before, lp_before
    for entry in transcript:
        if not banded_step(
            assets,
            x,
            k,
            lp,
            entry.assets,
            entry.x,
            entry.lp,
            entry.fee_budget,
            bands,
            closing,
            wt,
            index,
        ):
            return False
        assets, x, k, lp = entry.assets, entry.x, entry.k, entry.lp
    return final_boundary_bounded(assets, x, k, bands, closing, wt, index)


def bounded_pair(
    after: tuple[int, int],
    x_min: int,
    bands: list[dict],
    closing: tuple[int, int],
    wt: int,
    index: list | None = None,
) -> tuple[int, int] | None:
    """A pair ``(x, k)``, ``x >= x_min``, the last boundary may declare; else ``None``.

    Per band, P2, P3 and P6 hold up to some counter and P4, P5 from some
    counter on, so the largest counter at which P6 still holds is the one to try;
    it is found by doubling and bisection on the oracle's own predicates.
    """
    index = index if index is not None else build_index(bands, closing, wt)
    x_min = max(x_min, wt)

    def holds(x: int, k: int) -> bool:
        st = ladder_at_upper(bands, index, closing, wt, x, k)
        ra, rb = after[0] - st.ca, after[1] - st.cb
        return ra >= 0 and rb >= 0 and g_of(st, ra, rb, st.l_k) >= 0

    for k in range(len(bands)):
        if not holds(x_min, k):
            continue
        lo, step = x_min, 1
        while holds(lo + step, k):
            lo, step = lo + step, step * 2
        hi = lo + step
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if holds(mid, k):
                lo = mid
            else:
                hi = mid
        if final_boundary_bounded(after, lo, k, bands, closing, wt, index):
            return (lo, k)
    return None
