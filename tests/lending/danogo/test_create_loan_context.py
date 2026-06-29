"""CreateLoanContext must derive the same role indices the on-chain redeemer used."""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.transactions.context import CreateLoanContext
from charli3_dendrite.lending.danogo.transactions.redeemers import CreateLoan

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)


def _captured_create_loan():
    for r in FIX["redeemers"]:
        if r["purpose"] == "mint" and r["script_hash"] == FIX["loan_skh"]:
            return CreateLoan.from_cbor(bytes.fromhex(r["cbor"]))
    raise AssertionError("no loan mint redeemer")


def test_context_derives_same_indices_as_onchain():
    ctx = CreateLoanContext.from_fixture(FIX)
    want = _captured_create_loan()
    assert ctx.pool_out_idx == want.pool_out_idx
    assert ctx.loan_out_idx == want.loan_out_idx
    assert ctx.protocol_cfg_ref_idx == want.protocol_cfg_ref_idx
    assert ctx.market_ref_idx == want.market_ref_idx
    assert ctx.fee_out_idx == want.fee_out_idx.value
    assert ctx.pool_in_out_ref[0] == want.pool_in_out_ref.transaction_id.hex()
    assert ctx.pool_in_out_ref[1] == want.pool_in_out_ref.output_index


def test_context_carries_oracle_redeemer():
    ctx = CreateLoanContext.from_fixture(FIX)
    assert ctx.oracle_redeemer_cbor
