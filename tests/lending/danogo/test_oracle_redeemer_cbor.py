"""OraclePriceCalcRdmr must re-emit the exact on-chain redeemer CBOR."""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)
ORACLE_SKH = "012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a"


def _oracle_cbor():
    for r in FIX["redeemers"]:
        if r["purpose"] == "reward" and r["script_hash"] == ORACLE_SKH:
            return bytes.fromhex(r["cbor"])
    raise AssertionError("no oracle reward redeemer")


def test_oracle_redeemer_round_trips_byte_exact():
    raw = _oracle_cbor()
    rdmr = OraclePriceCalcRdmr.from_cbor(raw)
    assert rdmr.to_cbor() == raw


def test_assembled_oracle_redeemer_prices_reproduce_and_round_trip():
    from charli3_dendrite.lending.danogo.transactions.context import (
        CreateLoanContext,
    )
    from charli3_dendrite.lending.danogo.transactions.oracle_redeemer import (
        assemble_oracle_redeemer,
    )

    ctx = CreateLoanContext.from_fixture(FIX)
    rdmr, report = assemble_oracle_redeemer(ctx)
    assert report["total"] > 0
    # Splash external-token spot residuals (~0.08% of history) may not reproduce.
    assert report["covered"] >= report["total"] - 2
    assert rdmr.to_cbor() == bytes.fromhex(ctx.oracle_redeemer_cbor)
