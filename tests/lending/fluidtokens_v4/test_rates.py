"""FluidTokens V4 rate models against the contract-exact loan maths (offline)."""

from fractions import Fraction

import pytest

from charli3_dendrite.lending.fluidtokens.math import amortization_installment
from charli3_dendrite.lending.fluidtokens.math import installments_pi_amount
from charli3_dendrite.lending.fluidtokens.math import is_repayment_late
from charli3_dendrite.lending.fluidtokens.math import perpetual_debt
from charli3_dendrite.lending.fluidtokens.math import perpetual_outstanding_debt
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidAmortizedRate
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidFlatTermRate
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidPerpetualRate
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.rates import MS_PER_HOUR
from charli3_dendrite.lending.rates import RateKind
from charli3_dendrite.lending.rates import rate_model_from_dict

_DAY_MS = 24 * MS_PER_HOUR
AMOUNTS = (1, 1_000_000, 5_000_000_000)
DURATIONS = (0, 1, MS_PER_HOUR, 30 * _DAY_MS, 365 * _DAY_MS)


def _terms(**overrides):
    values = {
        "interest_rate": 1200,
        "total_installments": 12,
        "installment_period": 720,
        "initial_grace_period": 24,
        "repayment_time_window": 48,
        "penalty_fee_for_late_repayment": 1_000_000,
    }
    values.update(overrides)
    return values


def _perpetual(**overrides):
    values = _terms(
        interest_rate=743,
        total_installments=0,
        installment_period=0,
        initial_grace_period=0,
        repayment_time_window=0,
        penalty_fee_for_late_repayment=0,
    )
    values["apy_coef"] = 28
    values.update(overrides)
    return FluidPerpetualRate(**values)


@pytest.mark.parametrize("amount", AMOUNTS)
@pytest.mark.parametrize("duration", DURATIONS)
def test_perpetual_interest_is_the_contract_debt_less_the_amount(amount, duration):
    model = _perpetual()
    debt = perpetual_debt(
        principal=amount,
        interest_rate=743,
        apy_coef=28,
        elapsed_ms=duration,
    )
    assert model.interest_for(amount, duration) == debt - amount


@pytest.mark.parametrize("paid", [0, 2])
def test_perpetual_outstanding_is_the_contract_remaining_debt(paid):
    model = _perpetual(installment_period=720, initial_grace_period=24)
    for now in (0, 10, 5 * 30 * _DAY_MS):
        assert model.outstanding(
            7_000_000,
            opened_ms=10,
            now_ms=now,
            installments_paid=paid,
        ) == perpetual_outstanding_debt(
            principal=7_000_000,
            interest_rate=743,
            apy_coef=28,
            lend_date_ms=10,
            now_ms=now,
            repaid_installments=paid,
            installment_period=720,
            initial_grace_period=24,
        )


def test_perpetual_headline_is_the_rate_at_borrowing():
    model = _perpetual()
    assert model.kind is RateKind.PERPETUAL
    assert model.headline_rate() == Fraction(743, 10_000)
    assert model.term_hours() is None
    assert model.installment_period_hours() is None
    assert _perpetual(installment_period=720).installment_period_hours() == 720


def test_negative_durations_are_refused():
    with pytest.raises(ValueError, match="negative"):
        _perpetual().interest_for(1, -1)
    with pytest.raises(ValueError, match="negative"):
        FluidFlatTermRate(**_terms()).interest_for(1, -1)


@pytest.mark.parametrize("amount", AMOUNTS)
def test_flat_term_interest_is_the_whole_schedule(amount):
    model = FluidFlatTermRate(**_terms())
    installment = installments_pi_amount(
        principal=amount,
        interest_rate=1200,
        total_installments=12,
    )
    assert model.interest_for(amount, 0) == 12 * installment - amount
    assert model.interest_for(amount, 365 * _DAY_MS) == 12 * installment - amount
    assert (
        model.outstanding(
            amount,
            opened_ms=0,
            now_ms=0,
            installments_paid=3,
        )
        == 9 * installment
    )


@pytest.mark.parametrize("amount", AMOUNTS)
def test_amortized_interest_is_the_whole_annuity(amount):
    model = FluidAmortizedRate(**_terms())
    installment = amortization_installment(
        principal=amount,
        interest_rate=1200,
        total_installments=12,
    )
    assert model.interest_for(amount, 0) == 12 * installment - amount
    assert (
        model.outstanding(
            amount,
            opened_ms=0,
            now_ms=0,
            installments_paid=12,
        )
        == 0
    )


def test_term_headlines_annualize_over_the_term():
    term = 24 + 12 * 720
    flat = FluidFlatTermRate(**_terms())
    assert flat.term_hours() == term
    assert flat.headline_rate() == Fraction(1200, 10_000) * 8760 / term
    amortized = FluidAmortizedRate(**_terms())
    principal = 10**12
    realized = Fraction(amortized.interest_for(principal, 0), principal) * 8760 / term
    assert abs(amortized.headline_rate() - realized) < Fraction(1, 10**9)
    assert FluidAmortizedRate(**_terms(interest_rate=0)).headline_rate() == 0


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"total_installments": 0}, "needs installments"),
        ({"initial_grace_period": 0, "installment_period": 0}, "longer than 0"),
    ],
)
def test_a_term_schedule_the_contract_cannot_price_is_refused(overrides, match):
    for cls in (FluidFlatTermRate, FluidAmortizedRate):
        with pytest.raises(LendingParseError, match=match):
            cls(**_terms(**overrides))


@pytest.mark.parametrize("now_hours", [24 + 720, 24 + 720 + 48, 24 + 720 + 49])
def test_lateness_is_the_contracts_rule(now_hours):
    now = now_hours * MS_PER_HOUR
    for model, perpetual in (
        (FluidFlatTermRate(**_terms()), False),
        (
            _perpetual(
                installment_period=720,
                initial_grace_period=24,
                repayment_time_window=48,
            ),
            True,
        ),
    ):
        assert model.is_late(opened_ms=0, now_ms=now, installments_paid=0) == (
            is_repayment_late(
                is_perpetual=perpetual,
                now_ms=now,
                lend_date_ms=0,
                initial_grace_period=24,
                repaid_installments=0,
                installment_period=720,
                repayment_time_window=48,
            )
        )


@pytest.mark.parametrize(
    "model",
    [
        _perpetual(),
        FluidFlatTermRate(**_terms()),
        FluidAmortizedRate(**_terms()),
    ],
)
def test_every_model_rebuilds_from_its_record(model):
    assert rate_model_from_dict(model.to_dict()) == model
