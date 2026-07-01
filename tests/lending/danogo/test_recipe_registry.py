"""The committed recipe registry is a non-trivial, well-formed {key: Recipe} map."""
import json
from importlib.resources import files

import pytest

from charli3_dendrite.lending.danogo.oracles.locator import Recipe
from charli3_dendrite.lending.danogo.oracles.locator import registry_from_dict

REG_PATH = files("charli3_dendrite.lending.danogo.oracles").joinpath("recipes.json")
REG = registry_from_dict(json.loads(REG_PATH.read_text())) if REG_PATH.is_file() else {}


@pytest.mark.skipif(not REG, reason="oracle_recipes.json not mined (needs db-sync)")
def test_registry_is_nontrivial_and_well_formed():
    assert len(REG) >= 8
    for key, recipe in REG.items():
        assert isinstance(recipe, Recipe) and recipe.steps
        assert "|" in key  # "collateral|quote"
        for step in recipe.steps:
            assert step.handle.otype and step.handle.address
            assert isinstance(step.is_reverse, bool)
            assert isinstance(step.scale_exp, int)
