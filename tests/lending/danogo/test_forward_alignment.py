"""The captured single-path leaves, walked in hop order, reproduce the on-chain price."""
import json
from fractions import Fraction
from pathlib import Path

import pytest

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "forward_pricing.json").read_text()
)


def _walk(leaves) -> Fraction:
    from charli3_dendrite.lending.danogo.oracles.forward import leaf_rate

    price = Fraction(1)
    for lf in leaves:
        rate = leaf_rate(
            lf["otype"],
            lf["datum"],
            tuple((p, n, int(q)) for p, n, q in lf["assets"]),
            quote_unit="lovelace",
        )
        assert rate is not None, f"leaf {lf['otype']} did not parse"
        hop = (1 / rate) if lf["is_reverse"] else rate
        price *= hop * (Fraction(10) ** lf["scale_exp"])
    return price


@pytest.mark.skipif(not FIX, reason="forward_pricing.json not captured (needs db-sync)")
@pytest.mark.parametrize("case", FIX, ids=[c["tx"][:10] for c in FIX])
def test_single_path_leaves_reproduce_price(case):
    got = _walk(case["leaves"])
    assert got == Fraction(case["expected"][0], case["expected"][1])
