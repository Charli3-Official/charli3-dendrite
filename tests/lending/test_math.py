import pytest

from charli3_dendrite.lending.math import BASIS
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
