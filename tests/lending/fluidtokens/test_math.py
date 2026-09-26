from decimal import Decimal
from fractions import Fraction
from math import ceil

import pytest

from charli3_dendrite.lending.fluidtokens.math import amortization_installment
from charli3_dendrite.lending.fluidtokens.math import amortized_remaining_principal
from charli3_dendrite.lending.fluidtokens.math import health_factor
from charli3_dendrite.lending.fluidtokens.math import installments_pi_amount
from charli3_dendrite.lending.fluidtokens.math import is_repayment_late
from charli3_dendrite.lending.fluidtokens.math import liquidation_min_collateral
from charli3_dendrite.lending.fluidtokens.math import perpetual_debt
from charli3_dendrite.lending.fluidtokens.math import perpetual_outstanding_debt
from charli3_dendrite.lending.math import ceil_div

_MS_PER_HOUR = 3_600_000


def _contract_perpetual_debt(principal, interest_rate, apy_coef, elapsed_ms):
    """Independent reimplementation of finance.ak get_remaining_debt (PerpetualLoan)."""
    hours = Fraction(elapsed_ms, _MS_PER_HOUR)
    hours_sq = Fraction(elapsed_ms * elapsed_ms, _MS_PER_HOUR**2)
    c = Fraction(interest_rate, 10000)
    m = Fraction(apy_coef, 1_000_000)
    interest = Fraction(principal) * (c * hours + m * hours_sq) / 8760
    return ceil(Fraction(principal) + interest)


def test_perpetual_debt_grows_with_time():
    d0 = perpetual_outstanding_debt(
        principal=3_500_000_000,
        interest_rate=400,
        apy_coef=28,
        lend_date_ms=1_700_000_000_000,
        now_ms=1_700_000_000_000,
    )
    d1 = perpetual_outstanding_debt(
        principal=3_500_000_000,
        interest_rate=400,
        apy_coef=28,
        lend_date_ms=1_700_000_000_000,
        now_ms=1_700_000_000_000 + 3600_000 * 24 * 365,
    )
    assert d0 == 3_500_000_000
    assert d1 > d0


def test_perpetual_debt_matches_contract_formula():
    # Cross-check against an independent port of finance.ak: the quadratic apy term must
    # be the FULL m*H^2 (not the average-APY m*H^2/2 we previously used).
    elapsed = 1000 * _MS_PER_HOUR
    got = perpetual_outstanding_debt(
        principal=1_000_000,
        interest_rate=500,
        apy_coef=1000,
        lend_date_ms=0,
        now_ms=elapsed,
    )
    assert got == _contract_perpetual_debt(1_000_000, 500, 1000, elapsed)
    assert got == 1_119_864  # regression pin (the old halved formula gave 1_062_785)


def test_health_factor_liquidatable():
    hf = health_factor(
        collateral_value=Decimal(1000),
        debt=Decimal(1200),
        l_tv=100,
        l_tv_divider=125,
    )
    assert hf < 1


def test_amortization_installment_zero_rate_is_ceil_div():
    result = amortization_installment(
        principal=1_000_000,
        interest_rate=0,
        total_installments=12,
    )
    assert result == ceil_div(1_000_000, 12)
    assert result == 83_334


def test_amortization_installment_non_positive_installments_returns_principal():
    assert (
        amortization_installment(
            principal=1_000_000,
            interest_rate=1000,
            total_installments=0,
        )
        == 1_000_000
    )


def test_amortization_installment_normal_annuity():
    # P=1_000_000, interest_rate=1000 (=0.1 total); per-installment rate = 0.1/12, so
    # the annuity P*r*(1+r)^n / ((1+r)^n - 1), rounded up. (The contract divides the
    # rate across installments; the previous build used 0.1 per installment -> 146_764.)
    result = amortization_installment(
        principal=1_000_000,
        interest_rate=1000,
        total_installments=12,
    )
    assert result == 87_916


def test_installments_pi_amount_normal():
    # total interest = 1_000_000 * 1000/10_000 = 100_000;
    # ceil((1_000_000 + 100_000) / 12) = 91_667.
    result = installments_pi_amount(
        principal=1_000_000,
        interest_rate=1000,
        total_installments=12,
    )
    assert result == 91_667


def test_installments_pi_amount_non_positive_installments_returns_principal():
    assert (
        installments_pi_amount(
            principal=1_000_000,
            interest_rate=1000,
            total_installments=0,
        )
        == 1_000_000
    )


def test_health_factor_zero_debt_is_infinite():
    hf = health_factor(
        collateral_value=Decimal(1000),
        debt=Decimal(0),
        l_tv=100,
        l_tv_divider=125,
    )
    assert hf == Decimal("Infinity")


def test_health_factor_non_positive_divider_is_infinite():
    hf = health_factor(
        collateral_value=Decimal(1000),
        debt=Decimal(1200),
        l_tv=100,
        l_tv_divider=0,
    )
    assert hf == Decimal("Infinity")


def test_perpetual_debt_matches_mainnet_repayments():
    # A repay 107 s after lend date and a collateral change 4.5 h after it.
    assert (
        perpetual_debt(
            principal=20_000_000,
            interest_rate=452,
            apy_coef=28,
            elapsed_ms=107_000,
        )
        == 20_000_004
    )
    assert (
        perpetual_debt(
            principal=2_200_000_000,
            interest_rate=450,
            apy_coef=28,
            elapsed_ms=16_200_000,
        )
        == 2_200_050_999
    )


def test_perpetual_debt_before_lend_date_follows_the_contract():
    # A loan's lend date is its borrow's validity upper bound, so an action can
    # precede it; the contract's formula then dips below the principal.
    assert (
        perpetual_debt(
            principal=5_000_000_000,
            interest_rate=777,
            apy_coef=28,
            elapsed_ms=-20 * 60_000,
        )
        == 4_999_985_219
    )
    assert (
        perpetual_outstanding_debt(
            principal=5_000_000_000,
            interest_rate=777,
            apy_coef=28,
            lend_date_ms=1_200_000,
            now_ms=0,
        )
        == 5_000_000_000
    )


def test_perpetual_outstanding_debt_delegates_after_lend_date():
    assert perpetual_outstanding_debt(
        principal=2_200_000_000,
        interest_rate=450,
        apy_coef=28,
        lend_date_ms=0,
        now_ms=16_200_000,
    ) == perpetual_debt(
        principal=2_200_000_000,
        interest_rate=450,
        apy_coef=28,
        elapsed_ms=16_200_000,
    )


def test_repayment_is_late_strictly_after_its_window():
    terms = dict(
        is_perpetual=False,
        lend_date_ms=0,
        initial_grace_period=24,
        repaid_installments=1,
        installment_period=720,
        repayment_time_window=48,
    )
    deadline = (24 + 2 * 720 + 48) * _MS_PER_HOUR
    assert not is_repayment_late(now_ms=deadline, **terms)
    assert is_repayment_late(now_ms=deadline + 1, **terms)


def test_perpetual_loan_without_installments_is_never_late():
    assert not is_repayment_late(
        is_perpetual=True,
        now_ms=10**15,
        lend_date_ms=0,
        initial_grace_period=0,
        repaid_installments=0,
        installment_period=0,
        repayment_time_window=0,
    )


def test_amortized_remaining_principal():
    principal, rate, n = 20_000_000, 1200, 6
    assert (
        amortized_remaining_principal(
            principal=principal,
            interest_rate=rate,
            total_installments=n,
            repaid_installments=0,
        )
        == principal
    )
    installment = amortization_installment(
        principal=principal,
        interest_rate=rate,
        total_installments=n,
    )
    after_one = amortized_remaining_principal(
        principal=principal,
        interest_rate=rate,
        total_installments=n,
        repaid_installments=1,
    )
    assert after_one == ceil(principal * Fraction(102, 100) - installment)
    assert (
        amortized_remaining_principal(
            principal=principal,
            interest_rate=rate,
            total_installments=n,
            repaid_installments=n,
        )
        <= 0
    )


def test_amortized_remaining_principal_rejects_what_the_contract_cannot_compute():
    with pytest.raises(ValueError, match="non-zero rate"):
        amortized_remaining_principal(
            principal=1,
            interest_rate=0,
            total_installments=6,
            repaid_installments=1,
        )


def test_liquidation_min_collateral_matches_a_mainnet_loan():
    # 0.8 liquidation LTV, collateral priced at 2.25419077 lovelace per unit.
    assert (
        liquidation_min_collateral(
            debt=2_200_050_999,
            principal_price=Fraction(1),
            collateral_price=Fraction(225419077, 100000000),
            l_tv=100,
            l_tv_divider=125,
        )
        == 1_219_978_267
    )


def test_liquidation_min_collateral_rejects_a_worthless_collateral():
    with pytest.raises(ValueError, match="must be positive"):
        liquidation_min_collateral(
            debt=1,
            principal_price=Fraction(1),
            collateral_price=Fraction(0),
            l_tv=100,
            l_tv_divider=125,
        )
