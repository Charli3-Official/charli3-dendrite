from decimal import Decimal

from charli3_dendrite.lending.danogo.math import current_borrow_apy
from charli3_dendrite.lending.danogo.math import current_interest_index
from charli3_dendrite.lending.danogo.math import current_loan_amount
from charli3_dendrite.lending.danogo.math import total_collateral_val
from charli3_dendrite.lending.danogo.math import total_collateral_val_with_threshold
from charli3_dendrite.lending.danogo.math import util_rate

II = 1_000_000_000_000


def test_interest_index_no_time_elapsed_is_unchanged():
    assert (
        current_interest_index(II, borrow_apy=400, interest_time=1000, txn_time=1000)
        == II
    )


def test_interest_index_grows_with_time():
    out = current_interest_index(
        II, borrow_apy=400, interest_time=0, txn_time=31_536_000_000
    )
    assert out == II + II * 400 * 31_536_000_000 // (10_000 * 31_536_000_000)
    assert out == 1_040_000_000_000


def test_interest_index_txn_before_interest_time_is_unchanged():
    assert (
        current_interest_index(II, borrow_apy=400, interest_time=2000, txn_time=1000)
        == II
    )


def test_current_loan_amount_accrues_interest():
    assert (
        current_loan_amount(
            loan_amount=1_000_000, current_index=1_040_000_000_000, initial_index=II
        )
        == 1_040_000
    )


def test_total_collateral_val_floors_each_term():
    assert total_collateral_val([(3, 1, 3)]) == 1
    # Per-term floor(2/3)=0 twice -> 0; aggregate floor(4/3) would be 1.
    assert total_collateral_val([(2, 1, 3), (2, 1, 3)]) == 0


def test_collateral_val_with_threshold():
    assert total_collateral_val_with_threshold([(1000, 1, 1, 8000)]) == 800


def test_util_rate_zero_supply_is_zero():
    assert util_rate(total_borrow=0, total_supply=0) == Decimal(0)
    assert util_rate(total_borrow=850, total_supply=1000) == Decimal("0.85")


def test_borrow_apy_zero_supply_returns_base_rate():
    assert (
        current_borrow_apy(
            power_base=10_470, base_rate=400, total_borrow=0, total_supply=0
        )
        == 400
    )


def test_borrow_apy_increases_with_utilization():
    low = current_borrow_apy(
        power_base=10_470, base_rate=400, total_borrow=100, total_supply=1000
    )
    high = current_borrow_apy(
        power_base=10_470, base_rate=400, total_borrow=800, total_supply=1000
    )
    assert high > low >= 400


def test_borrow_apy_exact_value_at_80pct_util():
    exp = 80
    expected = (10_470**exp * 100) // (10_000**exp) + 400
    assert (
        current_borrow_apy(
            power_base=10_470, base_rate=400, total_borrow=800, total_supply=1000
        )
        == expected
    )


def test_borrow_apy_matches_live_mainnet_ada_pool():
    """Reproduce the on-chain borrow_apy of the live ADA pool.

    Pin both the formula (litepaper image) and the decoded market params
    (power_base=10250, base_rate=200): the ADA pool datum carried
    total_borrow=367903666430, total_supply=11113640493726, borrow_apy=307.
    """
    assert (
        current_borrow_apy(
            power_base=10_250,
            base_rate=200,
            total_borrow=367_903_666_430,
            total_supply=11_113_640_493_726,
        )
        == 307
    )
