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
chain to the base unit; a floating-point quote would not. The algorithms follow
the module specification and the integration reference implementation
(``banded-cl-integration.md`` §3-§4, ``banded-quote-reference.py``), and the
self-check vectors in the tests are real preview-network scoops.
"""

from __future__ import annotations

from dataclasses import dataclass

TWO64 = 1 << 64
"""The ladder index's fixed-point denominator."""

CURVE_CL = 0
"""``BandSpec.curve``: a concentrated-liquidity arc."""

CURVE_CONSTANT_SUM = 1
"""``BandSpec.curve``: a constant-sum bin."""

_WITNESS_SCAN = 400
"""How far around the achievable prefix's end the full band proof is scanned."""

_COUNTER_CEILING = 1 << 80
"""An upper bound on any ladder counter: the search's doubling stops here."""

Edge = tuple[int, int]
"""A sqrt-price edge ``p / q`` as ``(p, q)``, both positive."""

Fee = tuple[int, int]
"""A fee rate ``num / den``."""


def ceil_div(n: int, d: int) -> int:
    """``ceil(n / d)`` for a positive ``d``."""
    return -((-n) // d)


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
        ca.append(ceil_div(shared, weight_total * p0 * p1))
        cb.append(ceil_div(shared, weight_total * q0 * q1))
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
            ca=ceil_div(self.index[k][0] * counter, TWO64),
            cb=ceil_div(self.index[k][1] * counter, TWO64),
            liquidity=band.weight * counter // w_total,
            a_sat=ceil_div(shared, w_total * p0 * p1),
            b_sat=ceil_div(shared, w_total * q0 * q1),
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


def _witness_in_band(ladder: Ladder, a: int, b: int, k: int) -> Witness | None:
    """The witness in band ``k`` if the reserves admit one.

    ``{X : achievable}`` is a prefix in ``X``; its end is found by doubling then
    bisection, and the full proof is scanned outward from there.
    """
    w_total = ladder.weight_total
    if not achievable(ladder, a, b, w_total, k):
        return None
    lo = hi = w_total
    while achievable(ladder, a, b, hi, k) and hi < _COUNTER_CEILING:
        lo, hi = hi, hi * 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if achievable(ladder, a, b, mid, k):
            lo = mid
        else:
            hi = mid
    for offset in range(_WITNESS_SCAN):
        for counter in (lo,) if offset == 0 else (lo + offset, lo - offset):
            if counter < w_total:
                continue
            if is_witness(ladder, a, b, counter, k):
                state = ladder.at(counter, k)
                return Witness(
                    counter=counter,
                    band=k,
                    state=state,
                    ra=a - state.ca,
                    rb=b - state.cb,
                )
    return None


def find_witness(
    ladder: Ladder,
    a: int,
    b: int,
    prefer: int | None = None,
) -> Witness | None:
    """The witness for reserves ``(a, b)``, or ``None`` if no band admits one.

    ``prefer`` is the band the price is moving **into**. A state exactly on an
    edge is a witness in both neighbouring bands and only the one being entered
    has capacity left, so it is searched first; the rest are searched in index
    order.
    """
    if prefer is not None and 0 <= prefer < ladder.n:
        found = _witness_in_band(ladder, a, b, prefer)
        if found is not None:
            return found
    for k in range(ladder.n):
        if k == prefer:
            continue
        found = _witness_in_band(ladder, a, b, k)
        if found is not None:
            return found
    return None


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
class BandedQuote:
    """A quote across the ladder.

    ``amount_out`` is the total output, ``spent`` how much of the offer the ladder
    absorbed (less than offered only when the ladder ran out of bands or the
    state is unpriceable), ``bands`` the bands used in order, and
    ``reserves_after`` the ``(A, B)`` the pool is left with.
    """

    amount_out: int
    spent: int
    bands: tuple[int, ...]
    reserves_after: tuple[int, int]


def banded_quote(
    ladder: Ladder,
    a: int,
    b: int,
    dx: int,
    a_is_input: bool,
    witness: Witness | None = None,
) -> BandedQuote:
    """Total output for ``dx`` of the input asset, crossing bands as needed.

    The active band pays at most its own holding of the output asset (``rb`` when
    A is the input, ``ra`` when B is). When the band's formula would pay more, the
    largest input whose output fits is found by bisection, applied, and the
    remainder continues in the next band down (A input) or up (B input). The
    quote stops when the input is spent, the ladder runs out, or a band cannot
    absorb a single unit. ``witness`` may supply the already-known witness of
    ``(a, b)``.
    """
    out, left = 0, dx
    bands_used: list[int] = []
    prefer: int | None = None
    while left > 0:
        if witness is None:
            witness = find_witness(ladder, a, b, prefer)
            if witness is None:
                break
        k = witness.band
        band = ladder.bands[k]
        cap = witness.rb if a_is_input else witness.ra
        got = band_output(witness, band, a_is_input, left)
        if got <= cap:
            spend = left
        else:
            lo, hi = 0, left
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                if band_output(witness, band, a_is_input, mid) <= cap:
                    lo = mid
                else:
                    hi = mid
            spend, got = lo, band_output(witness, band, a_is_input, lo)
            if spend == 0:
                break
        out += got
        left -= spend
        bands_used.append(k)
        a, b = (a + spend, b - got) if a_is_input else (a - got, b + spend)
        prefer = k - 1 if a_is_input else k + 1
        witness = None
        if not 0 <= prefer < ladder.n:
            break
    return BandedQuote(
        amount_out=out,
        spent=dx - left,
        bands=tuple(bands_used),
        reserves_after=(a, b),
    )
