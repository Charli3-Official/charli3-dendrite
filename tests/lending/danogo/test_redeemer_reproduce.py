"""Reproduce real mainnet collateral prices from captured redeemer leaves.

Each fixture is one ``OraclePriceCalcRdmr`` reduced to its concrete leaf UTxOs
(``oracle_idxs`` -> source type + datum + value) and its ground-truth ``prices``. The
per-leaf parsers plus hop composition must reconstruct every price exactly.
"""

import json
from fractions import Fraction
from pathlib import Path

import pytest

from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.oracles.reproduce import reproduce_price

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "redeemer_reproduction.json").read_text()
)


def _leaves(entry):
    return [
        OracleLeaf(
            otype=OracleUtxoType[leaf["otype"]],
            datum=leaf["datum"],
            assets=tuple((p, n, int(q)) for p, n, q in leaf["assets"]),
        )
        for leaf in entry["leaves"]
    ]


def _price_cases():
    for entry in FIXTURES:
        leaves = _leaves(entry)
        for price in entry["prices"]:
            yield pytest.param(
                leaves,
                price,
                id=f"{entry['tx'][:10]}:{price['collateral'][:10]}",
            )


@pytest.mark.parametrize("leaves,price", list(_price_cases()))
def test_collateral_price_reproduces(leaves, price):
    result = reproduce_price(
        quote_unit=price["quote"],
        target=(price["num"], price["denom"]),
        leaves=leaves,
    )
    assert result is not None, "no leaf composition reproduced the price"
    # The reproduction's stated rate is exactly the on-chain price.
    assert Fraction(*result.rate) == Fraction(price["num"], price["denom"])


def test_every_fixture_price_is_covered():
    total = sum(len(e["prices"]) for e in FIXTURES)
    covered = sum(
        reproduce_price(
            quote_unit=p["quote"], target=(p["num"], p["denom"]), leaves=_leaves(e)
        )
        is not None
        for e in FIXTURES
        for p in e["prices"]
    )
    assert covered == total


def test_methods_exercise_product_identity_and_scale():
    # The captured set must include each reconstruction move at least once so the
    # branches stay covered: a plain product, a decimal-scaled product, and identity.
    methods = set()
    for entry in FIXTURES:
        leaves = _leaves(entry)
        for price in entry["prices"]:
            r = reproduce_price(
                quote_unit=price["quote"],
                target=(price["num"], price["denom"]),
                leaves=leaves,
            )
            if r is None:
                continue
            if r.method == "identity":
                methods.add("identity")
            elif r.scale_exp != 0:
                methods.add("scaled")
            else:
                methods.add("product")
    assert {"product", "scaled", "identity"} <= methods
