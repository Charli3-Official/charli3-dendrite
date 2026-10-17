from fractions import Fraction

import pytest

from charli3_dendrite.lending.math import BASIS
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
