"""The banded CL ladder math on preview pool P1 and on edge cases of the walk.

P1 (``622cee5f…``) is a preview pool at block 4732337: its ladder, the index its
config carries on chain, and the witness the scooper declared for its reserves.
Of its eight quotes, 1,000 tOKENA for 996 tOKENC is preview transaction
``1fa1fda1…``; ``test_sundae_v4_banded_cl_oracle.py`` runs all eight through the
validator's transcript check, and the recorded preview scoops in the pool tests
carry P1's on-chain index. The other cases pin how a quote crosses band edges: at
a crossing that would lower the counter, where the last band cannot be drained
to zero, and where a band can pay nothing.
"""

from __future__ import annotations

import pytest

from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import CURVE_CONSTANT_SUM
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import Ladder
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import achievable
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import band_output
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import banded_quote
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import find_witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import is_witness
from charli3_dendrite.dexs.amm.sundae_v4_banded_cl_math import marginal_price
from charli3_dendrite.lending.math import ceil_div

# Pool P1's ladder: eight CL bands of weight 1, sqrt-price edges 76000/80000 to
# 84000/80000 in steps of 1000/80000, fees 3/1000 both ways.
P1_STARTS = [(76000 + 1000 * i, 80000) for i in range(8)]
P1_CLOSING = (84000, 80000)
P1_RESERVES = (5_953_003, 6_249_988)  # A = tOKENA, B = tOKENC, block 4732337
P1_WITNESS = (1_000_049_919, 3)
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


# A ladder whose band 2 -> 3 edge re-derives a lower counter (all CL arcs).
DROP_LADDER = {
    "starts": [
        (4, 10),
        (6, 10),
        (7, 10),
        (9, 10),
        (10, 10),
        (11, 10),
        (12, 10),
        (14, 10),
    ],
    "closing": (16, 10),
    "weights": [1, 1, 3, 1, 1, 7, 2, 199],
    "curves": [0] * 8,
    "fees_buy": [
        (7, 997),
        (1, 100),
        (0, 1),
        (7, 997),
        (0, 1),
        (1, 100),
        (3, 1000),
        (5, 10000),
    ],
    "fees_sell": [
        (1, 10),
        (25, 10000),
        (1, 10),
        (1, 10),
        (1, 10),
        (25, 10000),
        (11, 1009),
        (0, 1),
    ],
    "weight_total": 215,
}
DROP_STATE = (428_916_515, 11_327_381)


def test_a_quote_stops_where_crossing_would_lower_the_counter() -> None:
    ladder = Ladder.from_shape(**DROP_LADDER)
    start = find_witness(ladder, *DROP_STATE)
    assert (start.band, start.counter) == (2, 4_777_717_382)
    quote = banded_quote(ladder, *DROP_STATE, 386_024_863, False)
    # Band 2 is bought out; band 3 admits the edge only at counter 4_777_717_377,
    # which one scoop cannot continue at, and band 2 has nothing left to sell.
    assert quote.bands == (2,)
    assert (quote.amount_out, quote.spent) == (12_515_654, 8_672_367)
    assert is_witness(ladder, *quote.reserves_after, 4_777_717_377, 3)
    assert banded_quote(ladder, *DROP_STATE, quote.spent, False).spent == quote.spent


# A single constant-sum bin whose output drains to zero one unit past its bound.
CS_BIN = {
    "starts": [(471, 1000)],
    "closing": (1459, 1000),
    "weights": [100],
    "curves": [CURVE_CONSTANT_SUM],
    "fees_buy": [(1, 100)],
    "fees_sell": [(0, 1)],
    "weight_total": 100,
}
CS_BIN_STATE = (842_063, 244_667)


def test_draining_the_last_band_stops_one_unit_short_of_an_unbounded_end() -> None:
    ladder = Ladder.from_shape(**CS_BIN)
    start = find_witness(ladder, *CS_BIN_STATE)
    assert start is not None
    quote = banded_quote(ladder, *CS_BIN_STATE, 367_000, True)
    # Paying all 244,667 B would leave A one unit past the bin's saturation at
    # every counter the walk may end on, so the last unit of B is not deliverable.
    assert quote.amount_out == 244_666
    assert quote.spent < 367_000
    end_a, end_b = quote.reserves_after
    assert end_b == 1
    state = ladder.at(start.counter, 0)
    assert achievable(ladder, end_a, end_b, start.counter, 0)
    assert end_a - state.ca <= state.a_sat


# A state three bands admit: band 4 is a sliver holding one unit of B, which no
# input buys on its own (one unit of A buys two of B).
SLIVER_LADDER = {
    "starts": [
        (69079, 80000),
        (120232, 80000),
        (120315, 80000),
        (120694, 80000),
        (120701, 80000),
        (120742, 80000),
        (139545, 80000),
        (139576, 80000),
        (156193, 80000),
    ],
    "closing": (162086, 80000),
    "weights": [2, 1, 1, 100, 1, 199, 100, 7, 100],
    "curves": [1, 0, 0, 0, 0, 0, 0, 0, 0],
    "fees_buy": [
        (1, 100),
        (1, 100),
        (7, 997),
        (7, 997),
        (1, 100),
        (1, 100),
        (3, 1000),
        (1, 100),
        (7, 997),
    ],
    "fees_sell": [
        (25, 10000),
        (11, 1009),
        (0, 1),
        (11, 1009),
        (25, 10000),
        (3, 1000),
        (3, 1000),
        (25, 10000),
        (11, 1009),
    ],
    "weight_total": 511,
}
SLIVER_STATE = (36_385, 2_346)


def test_a_band_that_pays_nothing_hands_the_trade_to_the_next_band() -> None:
    ladder = Ladder.from_shape(**SLIVER_LADDER)
    for band, counter in ((3, 926_473), (4, 926_483), (5, 926_463)):
        assert is_witness(ladder, *SLIVER_STATE, counter, band)
    sell = banded_quote(ladder, *SLIVER_STATE, 10, True)
    assert (sell.amount_out, sell.spent, sell.bands) == (15, 7, (3,))
    buy = banded_quote(ladder, *SLIVER_STATE, 10, False)
    assert (buy.amount_out, buy.spent, buy.bands) == (4, 10, (5,))
