"""The rate-model registry (offline)."""

from dataclasses import dataclass

import pytest

from charli3_dendrite.lending import rates
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidPerpetualRate
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
