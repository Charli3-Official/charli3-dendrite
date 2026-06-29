"""Recipe types, the observation->recipe transform, and registry round-tripping."""
import json
from pathlib import Path

from charli3_dendrite.lending.danogo.oracles.locator import LeafHandle
from charli3_dendrite.lending.danogo.oracles.locator import Recipe
from charli3_dendrite.lending.danogo.oracles.locator import RecipeStep
from charli3_dendrite.lending.danogo.oracles.locator import recipe_from_observation
from charli3_dendrite.lending.danogo.oracles.locator import registry_from_dict
from charli3_dendrite.lending.danogo.oracles.locator import registry_to_dict

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "forward_pricing.json").read_text()
)


def test_recipe_from_observation_builds_steps_with_handles():
    case = FIX[0]
    recipe = recipe_from_observation(case["leaves"])
    assert isinstance(recipe, Recipe)
    assert len(recipe.steps) == len(case["leaves"])
    for step, lf in zip(recipe.steps, case["leaves"]):
        assert isinstance(step, RecipeStep)
        assert isinstance(step.handle, LeafHandle)
        assert step.handle.otype == lf["otype"]
        assert step.handle.address == lf["address"]
        assert step.handle.nft_policy == lf["nft_policy"]
        assert step.handle.nft_name == lf["nft_name"]
        assert step.is_reverse == lf["is_reverse"]
        assert step.scale_exp == lf["scale_exp"]


def test_registry_round_trips_through_dict():
    registry = {c["token"]: recipe_from_observation(c["leaves"]) for c in FIX}
    again = registry_from_dict(registry_to_dict(registry))
    assert again == registry


def test_registry_to_dict_is_json_serializable():
    registry = {c["token"]: recipe_from_observation(c["leaves"]) for c in FIX}
    blob = json.dumps(registry_to_dict(registry))  # must not raise
    assert registry_from_dict(json.loads(blob)) == registry


def test_datum_key_round_trips_and_defaults_empty():
    keyed = Recipe(
        steps=(
            RecipeStep(
                handle=LeafHandle(
                    otype="TLIQWID_ORACLE_V2",
                    address="addr_test",
                    nft_policy="aa" * 28,
                    nft_name="",
                    datum_key="feedid:lovelace",
                ),
                is_reverse=True,
                scale_exp=0,
            ),
        ),
    )
    blob = registry_to_dict({"k": keyed})
    assert blob["k"]["steps"][0]["datum_key"] == "feedid:lovelace"
    assert registry_from_dict(blob) == {"k": keyed}

    # A step without a datum_key omits it from JSON and defaults back to "".
    plain = recipe_from_observation(FIX[0]["leaves"])
    plain_blob = registry_to_dict({"k": plain})
    assert "datum_key" not in plain_blob["k"]["steps"][0]
    assert registry_from_dict(plain_blob)["k"].steps[0].handle.datum_key == ""
