from decimal import Decimal
from fractions import Fraction
from math import ceil

from charli3_dendrite.lending.fluidtokens.math import amortization_installment
from charli3_dendrite.lending.fluidtokens.math import health_factor
from charli3_dendrite.lending.fluidtokens.math import installments_pi_amount
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
