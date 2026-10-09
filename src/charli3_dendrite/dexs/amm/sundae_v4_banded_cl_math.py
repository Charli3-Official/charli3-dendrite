"""Exact SundaeSwap V4 banded concentrated-liquidity math.

A banded pool is a ladder of ``N`` contiguous sqrt-price bands. Band ``k`` spans
``[edge_k, edge_{k+1}]`` where every edge is an exact rational square root of the
price (asset B per asset A). Each band holds a share ``w_k / W`` of the pool's
liquidity and prices either as a concentrated-liquidity arc (``curve == 0``) or
as a constant-sum bin (``curve == 1``). One band is active at a time; a swap
prices against it and crosses into the next band when it exhausts that band's
holding of the output asset.

The pool stores no price and no active band. Its state is the reserve pair
``(A, B)``; the *witness* ``(X, k)`` — the ladder counter (the pool's liquidity
scale) and the active band — is derived from the reserves on every spend by the
band proof (:func:`is_witness`), exactly as the on-chain module re-derives it.

Every quantity is a Python ``int``. Every division floors in the pool's favour,
and the ladder-index coefficients round up, so these functions reproduce the
chain to the base unit; a floating-point quote would not. They target the
deployed module (``banded_concentrated_liquidity.withdraw``), which reads the
prefix sums of a band proof from the config's ladder index; recorded preview
scoops of it replay through them step for step.

A quote is the transcript one scoop executes (:func:`banded_quote`): it threads
the witness from step to step as the module's transcript walk does, so the
counter never falls within a scoop and the walk's final boundary stays bounded.
"""

from __future__ import annotations

from dataclasses import dataclass

TWO64 = 1 << 64
"""The ladder index's fixed-point denominator."""

CURVE_CL = 0
"""``BandSpec.curve``: a concentrated-liquidity arc."""

CURVE_CONSTANT_SUM = 1
"""``BandSpec.curve``: a constant-sum bin."""

_WITNESS_SCAN = 2
"""How far around the achievable prefix's end the full band proof is checked."""

_COUNTER_CEILING = 1 << 80
"""An upper bound on any ladder counter: the search's doubling stops here."""

Edge = tuple[int, int]
"""A sqrt-price edge ``p / q`` as ``(p, q)``, both positive."""

Fee = tuple[int, int]
"""A fee rate ``num / den``."""


@dataclass(frozen=True)
class Band:
    """One band of the ladder: its edges, weight, curve and the two fees.

    ``lo`` and ``hi`` are the band's lower and upper sqrt-price edges;
    ``fee_sell`` is charged when the trade sells A (A is the input, the price
    falls) and ``fee_buy`` when it buys A (B is the input, the price rises).
    """

    lo: Edge
    hi: Edge
    weight: int
    curve: int
    fee_buy: Fee
    fee_sell: Fee

    @property
    def d(self) -> int:
        """``D = p_hi * q_lo - p_lo * q_hi``: positive exactly when ``lo < hi``."""
        (p0, q0), (p1, q1) = self.lo, self.hi
        return p1 * q0 - p0 * q1

    def fee(self, a_is_input: bool) -> Fee:
        """The fee the band charges for a trade in this direction."""
        return self.fee_sell if a_is_input else self.fee_buy


@dataclass(frozen=True)
class BandState:
    """Band ``k`` of a ladder at counter ``X``: what the on-chain step derives.

    ``ca`` is the A held by the bands above ``k`` and ``cb`` the B held by the
    bands below it (both from the ladder index, rounded up); ``liquidity`` is
    the band's own ``L = floor(w_k * X / W)``; ``a_sat`` / ``b_sat`` are its
    saturation constants, the most A (B) it can hold.
    """

    k: int
    ca: int
    cb: int
    liquidity: int
    a_sat: int
    b_sat: int


@dataclass(frozen=True)
class Witness:
    """The band proof's solution for a reserve pair: ``(X, k)`` and the residuals.

    ``ra`` / ``rb`` are the active band's own holdings, ``A - ca`` and ``B - cb``.
    """

    counter: int
    band: int
    state: BandState
    ra: int
    rb: int


def build_index(bands: list[Band], weight_total: int) -> list[tuple[int, int]]:
    """The ladder index: cumulative saturation coefficients at ``2**64``.

    Entry ``k`` is ``(sum of coeff_a_i for i > k, sum of coeff_b_i for i < k)``
    with ``coeff_a_i = ceil(w_i * D_i * 2**64 / (W * p_{i-1} * p_i))`` and
    ``coeff_b_i = ceil(w_i * D_i * 2**64 / (W * q_{i-1} * q_i))``. Each
    coefficient rounds **up** on its own, exactly as Create pins it.
    """
    ca: list[int] = []
    cb: list[int] = []
    for band in bands:
        (p0, q0), (p1, q1) = band.lo, band.hi
        shared = band.weight * band.d * TWO64
        ca.append(-(-shared // (weight_total * p0 * p1)))
        cb.append(-(-shared // (weight_total * q0 * q1)))
    return [(sum(ca[k + 1 :]), sum(cb[:k])) for k in range(len(bands))]


@dataclass(frozen=True)
class Ladder:
    """A banded ladder as the math sees it: bands, their total weight, the index.

    Build one with :meth:`from_shape` from the config fields (band starts, the
    closing edge, weights, curves and fees); the index is rebuilt from the
    shape, never trusted.
    """

    bands: tuple[Band, ...]
    weight_total: int
    index: tuple[tuple[int, int], ...]

    @classmethod
    def from_shape(
        cls,
        starts: list[Edge],
        closing: Edge,
        weights: list[int],
        curves: list[int],
        fees_buy: list[Fee],
        fees_sell: list[Fee],
        weight_total: int,
    ) -> Ladder:
        """A ladder from its config fields, validated as the module's Create does.

        Raises:
            ValueError: an empty ladder, a non-positive edge, edges that are not
                strictly increasing, a weight below one, an unknown curve tag, a fee
                outside ``[0, 1)``, or a ``weight_total`` that is not the weights'
                sum.
        """
        n = len(starts)
        if n == 0:
            msg = "banded_cl: an empty ladder has no active band."
            raise ValueError(msg)
        if not (n == len(weights) == len(curves) == len(fees_buy) == len(fees_sell)):
            msg = "banded_cl: the band fields are not aligned."
            raise ValueError(msg)
        edges = [*starts, closing]
        for p, q in edges:
            if p <= 0 or q <= 0:
                msg = f"banded_cl: a sqrt-price edge {p}/{q} is not positive."
                raise ValueError(msg)
        bands: list[Band] = []
        for k in range(n):
            (p0, q0), (p1, q1) = edges[k], edges[k + 1]
            if not p0 * q1 < p1 * q0:
                msg = f"banded_cl: edges {p0}/{q0} and {p1}/{q1} are not increasing."
                raise ValueError(msg)
            if weights[k] < 1:
                msg = f"banded_cl: band {k} has weight {weights[k]} below one."
                raise ValueError(msg)
            if curves[k] not in (CURVE_CL, CURVE_CONSTANT_SUM):
                msg = f"banded_cl: band {k} has unknown curve tag {curves[k]}."
                raise ValueError(msg)
            for fee in (fees_buy[k], fees_sell[k]):
                if not (fee[1] > 0 and 0 <= fee[0] < fee[1]):
                    msg = f"banded_cl: band {k} fee {fee[0]}/{fee[1]} is not in [0, 1)."
                    raise ValueError(msg)
            bands.append(
                Band(
                    lo=edges[k],
                    hi=edges[k + 1],
                    weight=weights[k],
                    curve=curves[k],
                    fee_buy=fees_buy[k],
                    fee_sell=fees_sell[k],
                ),
            )
        if sum(weights) != weight_total:
            msg = (
                f"banded_cl: weight_total {weight_total} is not the weights' sum "
                f"{sum(weights)}."
            )
            raise ValueError(msg)
        return cls(
            bands=tuple(bands),
            weight_total=weight_total,
            index=tuple(build_index(bands, weight_total)),
        )

    @property
    def n(self) -> int:
        """The band count."""
        return len(self.bands)

    def at(self, counter: int, k: int) -> BandState:
        """Band ``k``'s derived state at ``counter``."""
        band = self.bands[k]
        (p0, q0), (p1, q1) = band.lo, band.hi
        shared = band.weight * counter * band.d
        w_total = self.weight_total
        return BandState(
            k=k,
            ca=-(-self.index[k][0] * counter // TWO64),
            cb=-(-self.index[k][1] * counter // TWO64),
            liquidity=band.weight * counter // w_total,
            a_sat=-(-shared // (w_total * p0 * p1)),
            b_sat=-(-shared // (w_total * q0 * q1)),
        )


def curve_g(band: Band, ra: int, rb: int, liquidity: int) -> int:
    """The band's curve residual: ``>= 0`` when ``(ra, rb)`` is reachable at ``L``.

    A CL arc is ``(ra*p1 + L*q1) * (rb*q0 + L*p0) - L*L*p1*q0``; a constant-sum bin
    is ``ra*p0*p1 + rb*q0*q1 - L*D``.
    """
    (p0, q0), (p1, q1) = band.lo, band.hi
    if band.curve == CURVE_CL:
        return (ra * p1 + liquidity * q1) * (rb * q0 + liquidity * p0) - (
            liquidity * liquidity * p1 * q0
        )
    return ra * p0 * p1 + rb * q0 * q1 - liquidity * band.d


def achievable(ladder: Ladder, a: int, b: int, counter: int, k: int) -> bool:
    """P1, P2, P3 and P6 of the band proof: the prefix the bisection searches."""
    if counter < ladder.weight_total:
        return False
    state = ladder.at(counter, k)
    ra, rb = a - state.ca, b - state.cb
    if ra < 0 or rb < 0:
        return False
    return curve_g(ladder.bands[k], ra, rb, state.liquidity) >= 0


def is_witness(ladder: Ladder, a: int, b: int, counter: int, k: int) -> bool:
    """The full band proof P1-P7 for ``(counter, k)`` against reserves ``(a, b)``.

    P4/P5 keep the state inside band ``k``; P7 makes the counter tight: the curve
    residual at ``counter + 1`` is negative.
    """
    if not achievable(ladder, a, b, counter, k):
        return False
    state = ladder.at(counter, k)
    if a - state.ca > state.a_sat or b - state.cb > state.b_sat:
        return False
    above = ladder.at(counter + 1, k)
    return curve_g(ladder.bands[k], a - above.ca, b - above.cb, above.liquidity) < 0


def _prefix_end(ladder: Ladder, a: int, b: int, k: int, start: int) -> int | None:
    """The largest counter at which band ``k``'s prefix holds, or ``None`` if none.

    ``{X : achievable}`` is a prefix in ``X``: the prefix sums only grow with
    ``X``, so the residuals only shrink, and ``G`` does not grow. Its end is
    bracketed by galloping from ``start`` (up while the prefix holds, down while
    it does not, in steps that double from one) and then bisected.
    """
    w_total = ladder.weight_total
    step = 1
    if achievable(ladder, a, b, start, k):
        lo = start
        hi = lo + step
        while hi < _COUNTER_CEILING and achievable(ladder, a, b, hi, k):
            lo, step = hi, step * 2
            hi = lo + step
    else:
        hi = start
        while True:
            lo = max(hi - step, w_total)
            if achievable(ladder, a, b, lo, k):
                break
            if lo == w_total:
                return None
            hi, step = lo, step * 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if achievable(ladder, a, b, mid, k):
            lo = mid
        else:
            hi = mid
    return lo


def _witness_in_band(
    ladder: Ladder,
    a: int,
    b: int,
    k: int,
    near: int | None = None,
    floor: int | None = None,
) -> Witness | None:
    """Band ``k``'s witness for reserves ``(a, b)``, if the band admits one.

    P7 makes the counter tight, so the witness is the end of the achievable
    prefix; the full proof is checked there and one unit either side. The search
    starts at ``near`` when given (a counter the witness is close to, such as a
    neighbouring band's across an edge) and at the weight total otherwise.
    ``floor`` admits only a witness whose counter is at least ``floor``: as the
    prefix is a prefix, there is none unless the prefix holds at ``floor``.
    """
    w_total = ladder.weight_total
    least = w_total
    if floor is not None:
        if floor < w_total or not achievable(ladder, a, b, floor, k):
            return None
        least = floor
    start = least if near is None else max(near, least)
    end = _prefix_end(ladder, a, b, k, start)
    if end is None:
        return None
    for offset in range(_WITNESS_SCAN):
        for counter in (end,) if offset == 0 else (end + offset, end - offset):
            if counter >= least and is_witness(ladder, a, b, counter, k):
                state = ladder.at(counter, k)
                return Witness(
                    counter=counter,
                    band=k,
                    state=state,
                    ra=a - state.ca,
                    rb=b - state.cb,
                )
    return None


def _candidate_band(ladder: Ladder, a: int, b: int) -> int:
    """The band whose share of the ladder index brackets the reserves' ratio.

    At the edge between bands ``j - 1`` and ``j`` every band from ``j`` up holds
    only A and every band below only B, so there ``A : B`` is
    ``index[j - 1][0] : index[j][1]``, a ratio that falls as ``j`` rises. A state
    lies in the band above every edge whose ratio its own ``A : B`` does not
    exceed. Rounding can put a state just across an edge, so the neighbours are
    worth checking next.
    """
    index = ladder.index
    k = 0
    for j in range(1, ladder.n):
        if a * index[j][1] > b * index[j - 1][0]:
            break
        k = j
    return k


def find_witness(ladder: Ladder, a: int, b: int) -> Witness | None:
    """The witness for reserves ``(a, b)``, or ``None`` if no band admits one.

    Bands are searched from the one the index brackets the reserves in, then its
    neighbours, then every other band. A state exactly on an edge is a witness in
    both neighbouring bands; the first found is returned.
    """
    candidate = _candidate_band(ladder, a, b)
    seen: set[int] = set()
    for k in (candidate, candidate - 1, candidate + 1, *range(ladder.n)):
        if not 0 <= k < ladder.n or k in seen:
            continue
        seen.add(k)
        found = _witness_in_band(ladder, a, b, k)
        if found is not None:
            return found
    return None


def _capacity(witness: Witness, a_is_input: bool) -> int:
    """What the witness's band can still pay out in this direction."""
    return witness.rb if a_is_input else witness.ra


def _beyond(
    ladder: Ladder,
    a: int,
    b: int,
    a_is_input: bool,
    witness: Witness,
    floor: int | None,
) -> Witness | None:
    """The witness of the band beyond ``witness``'s in the trade's direction, if any.

    Only a band that admits the same state, at a counter of at least ``floor``.
    """
    k = witness.band - 1 if a_is_input else witness.band + 1
    if not 0 <= k < ladder.n:
        return None
    return _witness_in_band(ladder, a, b, k, near=witness.counter, floor=floor)


def directed_witness(
    ladder: Ladder,
    a: int,
    b: int,
    a_is_input: bool,
    witness: Witness | None = None,
) -> Witness | None:
    """The witness a trade in this direction starts from, or ``None``.

    The witness of ``(a, b)`` (``witness`` when already known), except on an edge
    where its band holds none of the output asset: there the band beyond the
    edge in the trade's direction, when it admits the same state.
    """
    if witness is None:
        witness = find_witness(ladder, a, b)
    if witness is None or _capacity(witness, a_is_input) > 0:
        return witness
    beyond = _beyond(ladder, a, b, a_is_input, witness, None)
    return witness if beyond is None else beyond


def _next_witness(
    ladder: Ladder,
    a: int,
    b: int,
    a_is_input: bool,
    previous: Witness,
) -> Witness | None:
    """The witness a transcript can continue from after a step priced at ``previous``.

    Within one scoop the counter may not fall from one boundary to the next (the
    budget pair needs ``fee_budget >= 0``), so only a witness whose counter is at
    least ``previous.counter`` is admissible. The band being entered is tried
    first, then the band just used, then every other band.
    """
    floor = previous.counter
    entering = previous.band - 1 if a_is_input else previous.band + 1
    order = [entering, previous.band]
    candidate = _candidate_band(ladder, a, b)
    order += [candidate, candidate - 1, candidate + 1, *range(ladder.n)]
    seen: set[int] = set()
    for k in order:
        if not 0 <= k < ladder.n or k in seen:
            continue
        seen.add(k)
        found = _witness_in_band(ladder, a, b, k, near=floor, floor=floor)
        if found is not None:
            return found
    return None


def _bounded(ladder: Ladder, a: int, b: int, k: int, floor: int) -> bool:
    """P1-P6 hold for ``(a, b)`` in band ``k`` at some counter of at least ``floor``.

    The bound the walk puts on its final boundary. The prefix (P1-P3, P6) holds
    up to its end and containment (P4, P5) from some counter on, so both hold
    somewhere at or above ``floor`` exactly when the prefix holds at ``floor``
    and containment holds at the prefix's end.
    """
    if not achievable(ladder, a, b, floor, k):
        return False
    state = ladder.at(floor, k)
    if a - state.ca > state.a_sat or b - state.cb > state.b_sat:
        end = _prefix_end(ladder, a, b, k, floor)
        if end is None:
            return False
        state = ladder.at(end, k)
    return a - state.ca <= state.a_sat and b - state.cb <= state.b_sat


def _final_bounded(
    ladder: Ladder,
    a: int,
    b: int,
    a_is_input: bool,
    last: Witness,
) -> bool:
    """A transcript may end at ``(a, b)`` after a step priced at ``last``.

    The walk accepts any band on the final boundary; the step's own band and
    the one beyond it are tried first.
    """
    entering = last.band - 1 if a_is_input else last.band + 1
    seen: set[int] = set()
    for k in (last.band, entering, *range(ladder.n)):
        if not 0 <= k < ladder.n or k in seen:
            continue
        seen.add(k)
        if _bounded(ladder, a, b, k, last.counter):
            return True
    return False


def virtual_reserves(witness: Witness, band: Band) -> tuple[int, int]:
    """A CL band's virtual reserves on its edges: ``(ra*p1 + L*q1, rb*q0 + L*p0)``.

    The band is a constant product on these; its marginal price (B per A) is
    ``VB * p1 / (VA * q0)``.
    """
    (p0, q0), (p1, q1) = band.lo, band.hi
    liquidity = witness.state.liquidity
    return (witness.ra * p1 + liquidity * q1, witness.rb * q0 + liquidity * p0)


def marginal_price(witness: Witness, band: Band, a_is_input: bool) -> tuple[int, int]:
    """The active band's fee-exclusive marginal rate as weights ``(p_in, p_out)``.

    Out per in at the margin is ``p_in / p_out``. A CL band prices at its virtual
    reserves; a constant-sum bin at the fixed ``(p0*p1) / (q0*q1)``.
    """
    (p0, q0), (p1, q1) = band.lo, band.hi
    if band.curve == CURVE_CL:
        va, vb = virtual_reserves(witness, band)
        b_per_a = (vb * p1, va * q0)
    else:
        b_per_a = (p0 * p1, q0 * q1)
    return b_per_a if a_is_input else (b_per_a[1], b_per_a[0])


def band_output(witness: Witness, band: Band, a_is_input: bool, dx: int) -> int:
    """What the active band pays for ``dx`` before its capacity is considered.

    The fee is taken from the input (``dx_eff = dx - floor(dx * fee)``); every
    division floors, in the pool's favour.
    """
    fee_num, fee_den = band.fee(a_is_input)
    dx_eff = dx - dx * fee_num // fee_den
    if dx_eff <= 0:
        return 0
    (p0, q0), (p1, q1) = band.lo, band.hi
    if band.curve == CURVE_CL:
        va, vb = virtual_reserves(witness, band)
        if a_is_input:
            d = dx_eff * p1
            return vb * d // ((va + d) * q0)
        d = dx_eff * q0
        return va * d // ((vb + d) * p1)
    price_num, price_den = p0 * p1, q0 * q1
    if a_is_input:
        return dx_eff * price_num // price_den
    return dx_eff * price_den // price_num


@dataclass(frozen=True)
class BandedStep:
    """One in-band step of a quote: what the chain sees as one transcript step.

    ``witness`` is the band proof for the reserves the step starts from,
    ``amount_in`` the input the band absorbed and ``amount_out`` what it paid.
    """

    witness: Witness
    amount_in: int
    amount_out: int


@dataclass(frozen=True)
class BandedQuote:
    """A quote across the ladder.

    ``amount_out`` is the total output, ``spent`` how much of the offer the ladder
    absorbs (less than offered when one scoop cannot fill all of it; see
    :func:`banded_quote`), ``bands`` the bands used in order, ``steps`` the
    per-band steps (every step pays a positive output), and ``reserves_after``
    the ``(A, B)`` the pool is left with.
    """

    amount_out: int
    spent: int
    bands: tuple[int, ...]
    reserves_after: tuple[int, int]
    steps: tuple[BandedStep, ...] = ()


def _fill(witness: Witness, band: Band, a_is_input: bool, dx: int, cap: int) -> int:
    """The largest input up to ``dx`` whose output in this band is at most ``cap``."""
    if band_output(witness, band, a_is_input, dx) <= cap:
        return dx
    lo, hi = 0, dx
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if band_output(witness, band, a_is_input, mid) <= cap:
            lo = mid
        else:
            hi = mid
    return lo


def _moved(
    a: int,
    b: int,
    a_is_input: bool,
    amount_in: int,
    amount_out: int,
) -> tuple[int, int]:
    return (
        (a + amount_in, b - amount_out)
        if a_is_input
        else (a - amount_out, b + amount_in)
    )


def banded_quote(
    ladder: Ladder,
    a: int,
    b: int,
    dx: int,
    a_is_input: bool,
    witness: Witness | None = None,
    drain: BandedQuote | None = None,
) -> BandedQuote:
    """Total output for ``dx`` of the input asset, crossing bands as needed.

    The quote is the transcript one scoop executes. The active band pays at most
    its own holding of the output asset (``rb`` when A is the input, ``ra`` when B
    is). When the band's formula would pay more, the largest input whose output
    fits is found by bisection, applied, and the remainder continues in the next
    band down (A input) or up (B input). A band that cannot pay one unit for the
    remaining input hands the trade to the next band in its direction that
    admits the same state. Every boundary after a step needs a witness whose
    counter is at least the step's (the budget pair needs ``fee_budget >= 0``),
    and the last boundary must satisfy P1-P6 at such a counter, which can make
    the last step absorb less than it could otherwise fill.

    The quote stops early, with ``spent < dx``, when one scoop cannot continue:
    the state is unpriceable, the ladder's last band is drained, the remaining
    input is too small to buy one unit, the band being entered admits the edge
    only at a lower counter, or it admits no witness there at all (integer
    rounding can exclude one side of an edge). A band may appear twice in a row
    in ``steps`` when a fill to its edge left residual units the output's jumps
    skipped over. ``witness`` may supply the already-known witness of ``(a, b)``.

    ``drain`` may supply a quote already made from the same reserves, direction
    and ``witness``, typically for the ledger-max offer. Every step of it but the
    last fills its band to the edge, so this quote makes each such step it
    outlasts exactly as that quote did; those are reused, not searched again.
    """
    if witness is None:
        witness = find_witness(ladder, a, b)
    if witness is None:
        return BandedQuote(amount_out=0, spent=0, bands=(), reserves_after=(a, b))
    return _walk(ladder, a, b, dx, a_is_input, witness, drain)


def _bounded_step(
    ladder: Ladder,
    a: int,
    b: int,
    a_is_input: bool,
    step: BandedStep,
) -> BandedStep | None:
    """``step`` from ``(a, b)``, cut to the largest input after which a walk may end.

    ``(a, b)`` is the state the step starts from, which its witness bounds, so a
    bisection on the input keeps a bound below and its failure above. ``None``
    when the input left buys nothing.
    """
    found = step.witness
    band = ladder.bands[found.band]
    lo, hi = 0, step.amount_in
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        after = _moved(a, b, a_is_input, mid, band_output(found, band, a_is_input, mid))
        if _final_bounded(ladder, *after, a_is_input, found):
            lo = mid
        else:
            hi = mid
    got = band_output(found, band, a_is_input, lo)
    return BandedStep(witness=found, amount_in=lo, amount_out=got) if got > 0 else None


def _walk(
    ladder: Ladder,
    a: int,
    b: int,
    dx: int,
    a_is_input: bool,
    start: Witness,
    drain: BandedQuote | None,
) -> BandedQuote:
    """The transcript :func:`banded_quote` describes, opening with ``start``."""
    a0, b0, left = a, b, dx
    steps: list[BandedStep] = []
    resume: Witness | None = start
    if drain is not None and drain.steps:
        for step in drain.steps[:-1]:
            if left <= step.amount_in:
                break
            steps.append(step)
            left -= step.amount_in
            a, b = _moved(a, b, a_is_input, step.amount_in, step.amount_out)
        resume = drain.steps[len(steps)].witness
    while left > 0:
        found: Witness | None
        if resume is not None:
            found, resume = resume, None
        else:
            found = _next_witness(ladder, a, b, a_is_input, steps[-1].witness)
        floor = steps[-1].witness.counter if steps else None
        got = 0
        while found is not None:
            band = ladder.bands[found.band]
            spend = _fill(found, band, a_is_input, left, _capacity(found, a_is_input))
            got = band_output(found, band, a_is_input, spend)
            if got > 0:
                break
            found = _beyond(ladder, a, b, a_is_input, found, floor)
        if found is None or got == 0:
            # Nothing more is paid: the band is exhausted and no admissible band
            # beyond it can continue, or the input is too small to buy one unit.
            # On chain an input that buys nothing is a donation, not a swap.
            break
        steps.append(BandedStep(witness=found, amount_in=spend, amount_out=got))
        left -= spend
        a, b = _moved(a, b, a_is_input, spend, got)
    if steps and not _final_bounded(ladder, a, b, a_is_input, steps[-1].witness):
        # The walk may not end after the last step: cut it until it may.
        last = steps.pop()
        a, b = _moved(a, b, a_is_input, -last.amount_in, -last.amount_out)
        left += last.amount_in
        cut = _bounded_step(ladder, a, b, a_is_input, last)
        if cut is not None:
            steps.append(cut)
            left -= cut.amount_in
            a, b = _moved(a, b, a_is_input, cut.amount_in, cut.amount_out)
    return BandedQuote(
        amount_out=(b0 - b) if a_is_input else (a0 - a),
        spent=dx - left,
        bands=tuple(step.witness.band for step in steps),
        reserves_after=(a, b),
        steps=tuple(steps),
    )
