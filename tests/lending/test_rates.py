"""The rate-model registry (offline)."""

from dataclasses import dataclass

import pytest

from charli3_dendrite.lending import rates
from charli3_dendrite.lending.fluidtokens_v4.rates import RATE_MODELS as FLUID_V4_MODELS
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidPerpetualRate
from charli3_dendrite.lending.liqwid.rates import RATE_MODELS as LIQWID_MODELS
from charli3_dendrite.lending.liqwid.rates import LiqwidVariableRate
from charli3_dendrite.lending.rates import HOURS_PER_YEAR
from charli3_dendrite.lending.rates import MS_PER_HOUR
from charli3_dendrite.lending.rates import DataclassIntParams
from charli3_dendrite.lending.rates import RateModel
from charli3_dendrite.lending.rates import rate_model_from_dict
from tests.lending.views import SimpleRate


def test_a_registered_model_rebuilds_from_its_record():
    record = SimpleRate(700).to_dict()
    assert record == {"protocol": "Test", "kind": "variable", "params": {"rate": 700}}
    assert rate_model_from_dict(record) == SimpleRate(700)


def test_an_unknown_model_names_the_known_ones():
    with pytest.raises(KeyError, match="Test/variable"):
        rate_model_from_dict({"protocol": "Nope", "kind": "variable", "params": {}})


def test_the_bundled_models_register_on_first_lookup(monkeypatch):
    monkeypatch.setattr(rates, "_REGISTRY", {})
    monkeypatch.setattr(rates, "_BUILTINS_LOADED", False)
    model = FluidPerpetualRate(
        interest_rate=743,
        total_installments=0,
        installment_period=0,
        initial_grace_period=0,
        repayment_time_window=0,
        penalty_fee_for_late_repayment=0,
        apy_coef=28,
    )
    assert rate_model_from_dict(model.to_dict()) == model


def test_a_model_registered_before_the_first_lookup_is_kept(monkeypatch):
    monkeypatch.setattr(rates, "_REGISTRY", {})
    monkeypatch.setattr(rates, "_BUILTINS_LOADED", False)

    @dataclass(frozen=True)
    class Mine(FluidPerpetualRate):
        pass

    rates.register_rate_model(Mine)
    record = Mine(
        interest_rate=1,
        total_installments=0,
        installment_period=0,
        initial_grace_period=0,
        repayment_time_window=0,
        penalty_fee_for_late_repayment=0,
        apy_coef=0,
    ).to_dict()
    assert type(rate_model_from_dict(record)) is Mine


def test_the_liquidation_debt_defaults_to_the_outstanding_debt():
    model = SimpleRate(1000)
    year_ms = HOURS_PER_YEAR * MS_PER_HOUR
    when = {"opened_ms": 0, "now_ms": year_ms, "installments_paid": 0}
    assert model.outstanding(1_000_000, **when) == 1_100_000
    assert model.liquidation_debt(1_000_000, **when) == 1_100_000


def test_a_model_keeps_itself_when_rebound_by_default():
    model = SimpleRate(1000)
    assert model.rebind(SimpleRate(2500)) is model


_LIQWID_PARAMS = {
    "rate_num": 3,
    "rate_den": 1000,
    "period_ms": 5_000,
    "market_index": 7,
    "loan_index": 5,
    "interest": 11,
    "min_interest": 2,
    "min_rate_num": 1,
    "min_rate_den": 1250,
}


def test_int_params_rebuild_every_field_as_an_int():
    loose = {**_LIQWID_PARAMS, "rate_num": "3", "period_ms": 5_000.0, "extra": 1}
    rebuilt = LiqwidVariableRate.from_params(loose)
    assert rebuilt.params() == _LIQWID_PARAMS
    assert all(type(value) is int for value in rebuilt.params().values())
    assert type(rebuilt) is LiqwidVariableRate


def test_int_params_need_every_field_including_defaulted_ones():
    partial = {k: v for k, v in _LIQWID_PARAMS.items() if k != "min_rate_den"}
    with pytest.raises(KeyError, match="min_rate_den"):
        LiqwidVariableRate.from_params(partial)


@pytest.mark.parametrize("model", [*FLUID_V4_MODELS, *LIQWID_MODELS])
def test_every_bundled_model_takes_params_from_the_mixin(model):
    assert issubclass(model, DataclassIntParams)
    assert issubclass(model, RateModel)
    assert model.params is DataclassIntParams.params
    assert model.from_params.__func__ is DataclassIntParams.from_params.__func__
    for cls in model.__mro__:
        if cls is not DataclassIntParams:
            assert "params" not in vars(cls) or cls is RateModel
            assert "from_params" not in vars(cls) or cls is RateModel


def test_a_liqwid_rate_round_trips_through_its_params():
    model = LiqwidVariableRate(
        rate_num=3,
        rate_den=1000,
        period_ms=5_000,
        market_index=7,
        loan_index=5,
        interest=11,
        min_interest=2,
    )
    assert model.params() == _LIQWID_PARAMS
    assert LiqwidVariableRate.from_params(model.params()) == model
    assert rate_model_from_dict(model.to_dict()) == model
