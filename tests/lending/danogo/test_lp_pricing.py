"""Tests for Danogo robust LP-token pricing (docs-confirmed leaf math)."""

from fractions import Fraction

import pytest

from charli3_dendrite.lending.danogo.oracles.lp_pricing import lp_price_is_consistent
from charli3_dendrite.lending.danogo.oracles.lp_pricing import lp_side_prices
from charli3_dendrite.lending.danogo.oracles.lp_pricing import lp_token_price


def _f(rational):
    num, den = rational
    return Fraction(num, den)


def test_fair_lp_price_balanced_pool():
    # Pool: 1000 A @ 1 X each, 2000 B @ 0.5 X each -> both sides hold 1000 X.
    # Total value 2000 X over 100 LP -> 20 X per LP.
    price = lp_token_price(
        reserve_a=1000,
        price_a=(1, 1),
        reserve_b=2000,
        price_b=(1, 2),
        total_lp=100,
    )
    assert _f(price) == Fraction(20)


def test_side_prices_equal_when_balanced():
    side_a, side_b = lp_side_prices(
        reserve_a=1000,
        price_a=(1, 1),
        reserve_b=2000,
        price_b=(1, 2),
        total_lp=100,
    )
    # Each side alone reconstructs the full fair price (2*1000/100 == 2*1000/100).
    assert _f(side_a) == _f(side_b) == Fraction(20)


def test_balanced_pool_is_consistent():
    assert lp_price_is_consistent(
        reserve_a=1000,
        price_a=(1, 1),
        reserve_b=2000,
        price_b=(1, 2),
        total_lp=100,
    )


def test_imbalanced_pool_is_rejected():
    # Side A holds 1000 X, side B holds only 500 X -> ~66% divergence > 5%.
    assert not lp_price_is_consistent(
        reserve_a=1000,
        price_a=(1, 1),
        reserve_b=1000,
        price_b=(1, 2),
        total_lp=100,
    )


def test_consistency_respects_tolerance_band():
    # ~4% divergence: rejected at default 5% only if outside; pick 1040 vs 1000.
    kwargs = dict(
        reserve_a=1000,
        price_a=(104, 100),
        reserve_b=1000,
        price_b=(1, 1),
        total_lp=100,
    )
    # midpoint relative deviation = 2*40/2040 ~= 3.92% -> within 5%, outside 3%.
    assert lp_price_is_consistent(tolerance_bps=500, **kwargs)
    assert not lp_price_is_consistent(tolerance_bps=300, **kwargs)


def test_rejects_non_positive_total_lp():
    with pytest.raises(ValueError):
        lp_token_price(
            reserve_a=1,
            price_a=(1, 1),
            reserve_b=1,
            price_b=(1, 1),
            total_lp=0,
        )
