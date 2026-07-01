"""CreateLoan redeemer must round-trip byte-exact against the captured tx."""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.transactions.redeemers import CreateLoan

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)


def _create_loan_mint_cbor():
    for r in FIX["redeemers"]:
        if r["purpose"] == "mint" and r["script_hash"] == FIX["loan_skh"]:
            return bytes.fromhex(r["cbor"])
    raise AssertionError("no loan mint redeemer")


def test_create_loan_round_trips_byte_exact():
    raw = _create_loan_mint_cbor()
    rdmr = CreateLoan.from_cbor(raw)
    assert rdmr.to_cbor() == raw


def test_create_loan_indices_are_nonnegative():
    rdmr = CreateLoan.from_cbor(_create_loan_mint_cbor())
    assert rdmr.pool_out_idx >= 0
    assert rdmr.loan_out_idx >= 0
    assert rdmr.protocol_cfg_ref_idx >= 0
    assert rdmr.market_ref_idx >= 0
