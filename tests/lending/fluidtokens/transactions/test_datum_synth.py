"""Byte-exact: the synthesized repayment-receipt datum reproduces the on-chain bytes."""
from __future__ import annotations

import json
from pathlib import Path

from charli3_dendrite.lending.fluidtokens.transactions.context import RepaySnapshot
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    synth_repayment_receipt,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def test_repayment_receipt_is_byte_exact() -> None:
    """The receipt synthesized from the loan datum equals the captured lender datum."""
    fix = _fixture("repay_full.json")
    snapshot = RepaySnapshot.from_capture(fix)
    receipt = synth_repayment_receipt(
        loan_datum=snapshot.loan_datum,
        loan_out_ref=snapshot.loan.out_ref,
        loan_id=snapshot.loan_id,
        lender_bond_policy=snapshot.lender_bond_policy,
    )
    assert receipt.to_cbor().hex() == fix["outputs"][0]["datum"]
