"""The rate-model registry (offline)."""

import pytest

from charli3_dendrite.lending.rates import rate_model_from_dict
from tests.lending.views import SimpleRate


def test_a_registered_model_rebuilds_from_its_record():
    record = SimpleRate(700).to_dict()
    assert record == {"protocol": "Test", "kind": "variable", "params": {"rate": 700}}
    assert rate_model_from_dict(record) == SimpleRate(700)


def test_an_unknown_model_names_the_known_ones():
    with pytest.raises(KeyError, match="Test/variable"):
        rate_model_from_dict({"protocol": "Nope", "kind": "variable", "params": {}})
