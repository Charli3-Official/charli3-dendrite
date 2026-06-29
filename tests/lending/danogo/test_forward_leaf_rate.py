"""leaf_rate dispatches to the verified per-source parsers."""
import json
from fractions import Fraction
from pathlib import Path

from charli3_dendrite.lending.danogo.oracles.forward import leaf_rate

REPRO = json.loads(
    (Path(__file__).parent / "fixtures" / "redeemer_reproduction.json").read_text()
)


def test_liqwid_oracle_v2_leaf_parses_to_positive_rate():
    leaf = next(
        lf for e in REPRO for lf in e["leaves"] if lf["otype"] == "TLIQWID_ORACLE_V2"
    )
    rate = leaf_rate(
        leaf["otype"],
        leaf["datum"],
        tuple((p, n, int(q)) for p, n, q in leaf["assets"]),
        quote_unit="lovelace",
    )
    assert rate is not None and rate > 0


def test_unparseable_leaf_returns_none():
    assert leaf_rate("TLIQWID_ORACLE_V2", None, (), quote_unit="lovelace") is None
