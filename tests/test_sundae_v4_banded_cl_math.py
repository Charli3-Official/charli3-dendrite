"""The banded CL ladder math against the preview chain.

The vectors are pool P1 (``622cee5f…``) at block 4732337 as the integration note
records them: its ladder, the index the module upgrade derived on chain, the
witness for its reserves, and eight quotes of which one (1,000 tOKENA for 996
tOKENC) is transaction ``1fa1fda1…``. The two config hashes are the pool's
``module_state`` before and after the upgrade, read from the live chain.
"""

from __future__ import annotations

import pytest

from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import CURVE_CONSTANT_SUM
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import TWO64
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Ladder
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import achievable
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import band_output
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import banded_quote
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import build_index
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import ceil_div
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import find_witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import is_witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import marginal_price

# Pool P1's ladder: eight CL bands of weight 1, sqrt-price edges 76000/80000 to
# 84000/80000 in steps of 1000/80000, fees 3/1000 both ways.
P1_STARTS = [(76000 + 1000 * i, 80000) for i in range(8)]
P1_CLOSING = (84000, 80000)
P1_RESERVES = (5_953_003, 6_249_988)  # A = tOKENA, B = tOKENC, block 4732337
P1_WITNESS = (1_000_049_919, 3)
P1_INDEX = [
    (199640087377809004, 0),
    (168926227781223003, 28823037615171175),
    (138989934250373357, 57646075230342350),
    (109802048057794952, 86469112845513525),
    (81334850413181446, 115292150460684700),
    (53561974662339001, 144115188075855875),
    (26458324833203603, 172938225691027050),
    (0, 201761263306198225),
]
# (input, A is the input, output, bands used)
P1_QUOTES = [
    (100, True, 99, (3,)),
    (1_000, True, 996, (3,)),
    (10_000, True, 9_969, (3,)),
    (1_000_000, True, 989_106, (3,)),
    (5_000_000, True, 4_793_722, (3, 2, 1, 0)),
    (1_000, False, 997, (3, 4)),
    (1_000_000, False, 989_117, (3, 4)),
    (5_000_000, False, 4_793_778, (3, 4, 5, 6, 7)),
]


def p1_ladder() -> Ladder:
    n = len(P1_STARTS)
    return Ladder.from_shape(
        starts=P1_STARTS,
        closing=P1_CLOSING,
        weights=[1] * n,
        curves=[0] * n,
        fees_buy=[(3, 1000)] * n,
        fees_sell=[(3, 1000)] * n,
        weight_total=n,
    )


def test_the_index_is_the_one_the_upgrade_derived_on_chain() -> None:
    ladder = p1_ladder()
    assert list(ladder.index) == P1_INDEX
    assert build_index(list(ladder.bands), ladder.weight_total) == P1_INDEX


def test_index_coefficients_round_up_per_band() -> None:
    ladder = p1_ladder()
    # index[k].0 sums the coefficients of the bands ABOVE k, so the difference
    # between entries 0 and 1 is band 1's own coefficient.
    band = ladder.bands[1]
    (p0, q0), (p1, q1) = band.lo, band.hi
    exact_a = band.weight * band.d * TWO64 / (ladder.weight_total * p0 * p1)
    coeff_a = ladder.index[0][0] - ladder.index[1][0]
    assert coeff_a == ceil_div(
        band.weight * band.d * TWO64, ladder.weight_total * p0 * p1
    )
    assert coeff_a >= exact_a
    assert 0 <= coeff_a - exact_a < 1
    assert ladder.index[-1][0] == 0
    assert ladder.index[0][1] == 0


def test_the_witness_for_the_recorded_reserves() -> None:
    ladder = p1_ladder()
    witness = find_witness(ladder, *P1_RESERVES)
    assert witness is not None
    assert (witness.counter, witness.band) == P1_WITNESS
    assert (witness.ra, witness.rb) == (324, 1_562_254)
    assert witness.state.liquidity == 125_006_239
    assert is_witness(ladder, *P1_RESERVES, witness.counter, witness.band)
    # The counter is tight: one more is not achievable, one less is not a witness.
    assert not achievable(ladder, *P1_RESERVES, witness.counter + 1, witness.band)
    assert not is_witness(ladder, *P1_RESERVES, witness.counter - 1, witness.band)
    # No other band admits a witness for a state strictly inside band 3.
    for k in range(ladder.n):
        if k != witness.band:
            assert find_witness(ladder, *P1_RESERVES, prefer=k).band == witness.band


@pytest.mark.parametrize(("dx", "a_in", "want", "bands"), P1_QUOTES)
def test_quotes_reproduce_the_preview_chain(
    dx: int, a_in: bool, want: int, bands: tuple[int, ...]
) -> None:
    quote = banded_quote(p1_ladder(), *P1_RESERVES, dx, a_in)
    assert quote.amount_out == want
    assert quote.bands == bands
    assert quote.spent == dx
    a, b = P1_RESERVES
    assert quote.reserves_after == ((a + dx, b - want) if a_in else (a - want, b + dx))


def test_a_quote_never_exceeds_the_band_capacity_and_crossing_is_exact() -> None:
    ladder = p1_ladder()
    a, b = P1_RESERVES
    witness = find_witness(ladder, a, b)
    assert witness is not None
    # Band 3 holds only 324 A, so buying 1,000 A must cross into band 4.
    one_band = band_output(witness, ladder.bands[3], False, 1_000)
    assert one_band > witness.ra
    quote = banded_quote(ladder, a, b, 1_000, False)
    assert quote.bands == (3, 4)
    # The crossing fills band 3 to its edge: after the first step its A is 0.
    first = banded_quote(ladder, a, b, 1, False)
    assert first.bands == (3,)
    # Monotone: a larger offer never pays less.
    outs = [
        banded_quote(ladder, a, b, dx, True).amount_out for dx in range(1, 2_000, 37)
    ]
    assert outs == sorted(outs)


def test_a_ledger_sized_offer_drains_the_pool_exactly() -> None:
    ladder = p1_ladder()
    a, b = P1_RESERVES
    sell_a = banded_quote(ladder, a, b, 2**64 - 1, True)
    assert sell_a.amount_out == b
    assert sell_a.bands == (3, 2, 1, 0)
    assert sell_a.spent < 2**64 - 1
    assert sell_a.reserves_after[1] == 0
    buy_a = banded_quote(ladder, a, b, 2**64 - 1, False)
    assert buy_a.amount_out == a
    assert buy_a.bands == (3, 4, 5, 6, 7)
    assert buy_a.reserves_after[0] == 0


def test_marginal_price_sits_inside_the_active_band() -> None:
    ladder = p1_ladder()
    witness = find_witness(ladder, *P1_RESERVES)
    assert witness is not None
    band = ladder.bands[witness.band]
    p_in, p_out = marginal_price(witness, band, True)  # B per A
    (p0, q0), (p1, q1) = band.lo, band.hi
    # sqrt-price edges: lo^2 <= price <= hi^2, cross-multiplied.
    assert p0 * p0 * p_out <= p_in * q0 * q0
    assert p_in * q1 * q1 <= p1 * p1 * p_out
    # The small-trade quote approaches the fee-adjusted marginal rate.
    out = banded_quote(ladder, *P1_RESERVES, 100, True).amount_out
    assert out <= 100 * p_in // p_out
    assert out >= 100 * p_in * 997 // (p_out * 1000) - 1
    flipped = marginal_price(witness, band, False)
    assert flipped == (p_out, p_in)


def test_a_constant_sum_bin_pays_at_its_fixed_price() -> None:
    ladder = Ladder.from_shape(
        starts=[(1, 1), (11, 10)],
        closing=(12, 10),
        weights=[1, 1],
        curves=[CURVE_CONSTANT_SUM, 0],
        fees_buy=[(0, 1), (0, 1)],
        fees_sell=[(0, 1), (0, 1)],
        weight_total=2,
    )
    # Band 0 is a bin priced at lo*hi = 1.1 B per A; build a state inside it.
    counter = 10**9
    state = ladder.at(counter, 0)
    ra = state.a_sat // 2
    rb = ceil_div(state.liquidity * ladder.bands[0].d - ra * 1 * 11, 10 * 10)
    a, b = ra + state.ca, rb + state.cb
    witness = find_witness(ladder, a, b)
    assert witness is not None
    assert witness.band == 0
    assert marginal_price(witness, ladder.bands[0], True) == (11, 10)
    assert band_output(witness, ladder.bands[0], True, 1_000) == 1_100
    assert band_output(witness, ladder.bands[0], False, 1_100) == 1_000


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"starts": [], "weights": [], "curves": [], "fees": []}, "empty ladder"),
        ({"starts": [(1, 1), (1, 1)]}, "not increasing"),
        ({"starts": [(0, 1), (2, 1)]}, "not positive"),
        ({"weights": [0, 1]}, "below one"),
        ({"curves": [2, 0]}, "unknown curve"),
        ({"fees": [(1, 1), (0, 1)]}, "not in [0, 1)"),
        ({"weight_total": 3}, "weights' sum"),
    ],
)
def test_malformed_shapes_are_rejected_as_create_rejects_them(
    kwargs: dict, message: str
) -> None:
    starts = kwargs.get("starts", [(1, 1), (2, 1)])
    n = len(starts)
    weights = kwargs.get("weights", [1] * n)
    fees = kwargs.get("fees", [(3, 1000)] * n)
    with pytest.raises(
        ValueError, match=message.replace("[", r"\[").replace(")", r"\)")
    ):
        Ladder.from_shape(
            starts=starts,
            closing=(3, 1),
            weights=weights,
            curves=kwargs.get("curves", [0] * n),
            fees_buy=fees,
            fees_sell=fees,
            weight_total=kwargs.get("weight_total", sum(weights)),
        )
