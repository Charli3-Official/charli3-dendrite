from decimal import Decimal

from charli3_dendrite.lending.fluidtokens.math import (
    amortization_installment,
    health_factor,
    installments_pi_amount,
    perpetual_outstanding_debt,
)
from charli3_dendrite.lending.math import ceil_div


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


def test_health_factor_liquidatable():
    hf = health_factor(
        collateral_value=Decimal(1000), debt=Decimal(1200), l_tv=100, l_tv_divider=125
    )
    assert hf < 1


def test_amortization_installment_zero_rate_is_ceil_div():
    result = amortization_installment(
        principal=1_000_000, interest_rate=0, total_installments=12
    )
    assert result == ceil_div(1_000_000, 12)
    assert result == 83_334


def test_amortization_installment_non_positive_installments_returns_principal():
    assert (
        amortization_installment(
            principal=1_000_000, interest_rate=1000, total_installments=0
        )
        == 1_000_000
    )


def test_amortization_installment_normal_annuity():
    # P=1_000_000, r=0.1/installment, n=12: P*r/(1-(1+r)^-n), rounded up.
    result = amortization_installment(
        principal=1_000_000, interest_rate=1000, total_installments=12
    )
    assert result == 146_764


def test_installments_pi_amount_normal():
    # interest = floor(1_000_000 * 1000 / 10_000) = 100_000;
    # ceil((1_000_000 + 100_000) / 12) = 91_667.
    result = installments_pi_amount(
        principal=1_000_000, interest_rate=1000, total_installments=12
    )
    assert result == 91_667


def test_installments_pi_amount_non_positive_installments_returns_principal():
    assert (
        installments_pi_amount(
            principal=1_000_000, interest_rate=1000, total_installments=0
        )
        == 1_000_000
    )


def test_health_factor_zero_debt_is_infinite():
    hf = health_factor(
        collateral_value=Decimal(1000), debt=Decimal(0), l_tv=100, l_tv_divider=125
    )
    assert hf == Decimal("Infinity")


def test_health_factor_non_positive_divider_is_infinite():
    hf = health_factor(
        collateral_value=Decimal(1000), debt=Decimal(1200), l_tv=100, l_tv_divider=0
    )
    assert hf == Decimal("Infinity")
