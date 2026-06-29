"""replay_recipe reproduces captured on-chain prices from the fixed leaf set."""
import json
from fractions import Fraction
from pathlib import Path

import pytest

from charli3_dendrite.lending.danogo.oracles.forward import LiveLeaf
from charli3_dendrite.lending.danogo.oracles.forward import replay_recipe

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "forward_pricing.json").read_text()
)


def _steps(case):
    return [
        (
            LiveLeaf(
                otype=lf["otype"],
                datum=lf["datum"],
                assets=tuple((p, n, int(q)) for p, n, q in lf["assets"]),
            ),
            lf["is_reverse"],
            lf["scale_exp"],
        )
        for lf in case["leaves"]
    ]


@pytest.mark.skipif(not FIX, reason="forward_pricing.json not captured")
@pytest.mark.parametrize("case", FIX, ids=[c["tx"][:10] for c in FIX])
def test_replay_recipe_reproduces_price(case):
    got = replay_recipe(_steps(case), quote_unit="lovelace")
    assert got is not None
    assert Fraction(got[0], got[1]) == Fraction(
        case["expected"][0], case["expected"][1]
    )


def test_replay_recipe_returns_none_on_unparseable_leaf():
    steps = [(LiveLeaf(otype="TLIQWID_ORACLE_V2", datum=None, assets=()), False, 0)]
    assert replay_recipe(steps, quote_unit="lovelace") is None
