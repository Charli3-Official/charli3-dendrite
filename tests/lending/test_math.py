from fractions import Fraction

import pytest

from charli3_dendrite.lending.math import BASIS
from charli3_dendrite.lending.math import apportion
from charli3_dendrite.lending.math import borrow_headroom
from charli3_dendrite.lending.math import bps_mul_ceil
from charli3_dendrite.lending.math import bps_mul_floor
from charli3_dendrite.lending.math import ceil_div
from charli3_dendrite.lending.math import floor_div


def test_basis_is_bps():
    assert BASIS == 10_000


def test_floor_div_rounds_down():
    assert floor_div(7, 2) == 3
    assert floor_div(10, 5) == 2


def test_ceil_div_rounds_up():
    assert ceil_div(7, 2) == 4
    assert ceil_div(10, 5) == 2


def test_bps_helpers():
    # 4% of 1_000_000 = 40_000 exactly
    assert bps_mul_floor(1_000_000, 400) == 40_000
    assert bps_mul_ceil(1_000_000, 400) == 40_000
    # 1/3 of a bps unit forces a rounding difference
    assert bps_mul_floor(1, 1) == 0
    assert bps_mul_ceil(1, 1) == 1


def test_zero_denominator_raises():
    with pytest.raises(ZeroDivisionError):
        floor_div(1, 0)
    with pytest.raises(ZeroDivisionError):
        ceil_div(1, 0)


@pytest.mark.parametrize(
    ("available", "principal", "cap", "expected"),
    [
        # 4/5 of 4,000,000 supplied is 3,200,000; 3,000,000 is already lent
        (1_000_000, 3_000_000, Fraction(4, 5), 200_000),
        # 4.7 floors to 4, never rounds up
        (10, 0, Fraction(47, 100), 4),
        # principal above the cap: 8,750 - 9,000 is negative
        (1_000, 9_000, Fraction(7, 8), 0),
        # -0.5 floors to -1 and still clamps to 0
        (0, 1, Fraction(1, 2), 0),
        # a zero cap disables borrowing
        (5_000_000, 2_000_000, Fraction(0), 0),
        # a cap of 1 frees exactly the available funds
        (1_000_000, 0, Fraction(1), 1_000_000),
        # the headroom itself is not bounded by the available funds
        (1_000_000, 1_000_000, Fraction(3, 2), 2_000_000),
    ],
)
def test_borrow_headroom_floors_and_never_goes_negative(
    available, principal, cap, expected
):
    assert borrow_headroom(available, principal, cap) == expected


def test_apportion_gives_each_weight_its_share_and_sums_exactly():
    # 10 * 1/7, 2/7, 4/7 = 1.43, 2.86, 5.71: floors 1, 2, 5 leave 2 units for
    # the two largest remainders (indices 1 and 2).
    assert apportion(10, [Fraction(1), Fraction(2), Fraction(4)]) == [1, 3, 6]


def test_apportion_breaks_remainder_ties_toward_the_lower_index():
    thirds = [Fraction(1, 3), Fraction(1, 3), Fraction(1, 3)]
    assert apportion(100, thirds) == [34, 33, 33]
    assert apportion(1, [Fraction(1, 2), Fraction(1, 2)]) == [1, 0]
    assert apportion(3, [Fraction(1)] * 4) == [1, 1, 1, 0]


def test_apportion_gives_a_zero_weight_nothing():
    weights = [Fraction(0), Fraction(1), Fraction(0), Fraction(1)]
    assert apportion(5, weights) == [0, 3, 0, 2]


def test_apportion_of_nothing_is_all_zeros():
    assert apportion(0, [Fraction(1), Fraction(2)]) == [0, 0]


def test_apportion_parts_stay_within_one_unit_of_the_exact_share():
    weights = [Fraction(1, 7), Fraction(2, 7), Fraction(0), Fraction(4, 7)]
    for total in range(200):
        parts = apportion(total, weights)
        assert sum(parts) == total
        for part, weight in zip(parts, weights):
            assert abs(part - total * weight) < 1


@pytest.mark.parametrize(
    ("total", "weights", "message"),
    [
        (5, [], "at least one weight"),
        (5, [Fraction(0), Fraction(0)], "not all be zero"),
        (5, [Fraction(-1), Fraction(2)], "non-negative"),
        (-1, [Fraction(1)], "negative total"),
    ],
)
def test_apportion_refuses_a_split_it_cannot_make(total, weights, message):
    with pytest.raises(ValueError, match=message):
        apportion(total, weights)
