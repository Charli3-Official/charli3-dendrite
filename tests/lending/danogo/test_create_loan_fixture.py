"""Sanity checks on the captured reference create-loan tx fixture."""

import json
from pathlib import Path

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)
ORACLE_SKH = "012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a"


def test_fixture_is_a_create_loan_tx():
    qty1 = [m for m in FIX["mints"] if m[2] == 1]
    assert len(qty1) == 2
    assert {m[0] for m in qty1} == {FIX["loan_skh"]}
    purposes = {(r["purpose"], r["script_hash"]) for r in FIX["redeemers"]}
    assert ("reward", ORACLE_SKH) in purposes
    assert ("mint", FIX["loan_skh"]) in purposes
    assert any(r["purpose"] == "spend" for r in FIX["redeemers"])


def test_fixture_has_loan_output_and_resolved_inputs():
    loan_outs = [
        o
        for o in FIX["outputs"].values()
        if any(a[0] == FIX["loan_skh"] and a[2] == "1" for a in o["assets"])
    ]
    assert loan_outs, "no loan UTxO in outputs"
    assert FIX["inputs"], "no spent inputs resolved"
    assert all(u.get("out_ref") for u in FIX["inputs"])
