"""DecreaseLoanAmount redeemer must round-trip byte-exact against the captured txs.

Full repay carries ``loan_out_idx = None`` (no loan output); partial repay carries
``loan_out_idx = Some``. Both always carry a fee output (``fee_out_idx = Some``).
"""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.transactions.redeemers import DecreaseLoanAmount
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import OutputReference
from charli3_dendrite.lending.danogo.transactions.redeemers import SomeInt

FIX_DIR = Path(__file__).parent / "fixtures"

FULL = json.loads((FIX_DIR / "decrease_loan_tx.json").read_text())
PARTIAL = json.loads((FIX_DIR / "decrease_loan_partial_tx.json").read_text())


def test_full_repay_round_trips_byte_exact():
    cbor = FULL["decrease_loan_redeemer"]
    rdmr = DecreaseLoanAmount.from_cbor(bytes.fromhex(cbor))
    assert rdmr.to_cbor().hex() == cbor


def test_partial_repay_round_trips_byte_exact():
    cbor = PARTIAL["decrease_loan_redeemer"]
    rdmr = DecreaseLoanAmount.from_cbor(bytes.fromhex(cbor))
    assert rdmr.to_cbor().hex() == cbor


def test_full_repay_decoded_fields_match_fixture():
    rdmr = DecreaseLoanAmount.from_cbor(bytes.fromhex(FULL["decrease_loan_redeemer"]))
    dec = FULL["decrease_loan_redeemer_decoded"]
    assert rdmr.CONSTR_ID == 5 == dec["constr"]
    assert rdmr.pool_out_idx == dec["pool_out_idx"]
    # Full repay closes the loan: no loan output, so loan_out_idx is None.
    assert isinstance(rdmr.loan_out_idx, NoneVal)
    assert dec["loan_out_idx"] is None
    # A fee output is always present on repay.
    assert isinstance(rdmr.fee_out_idx, SomeInt)
    assert rdmr.fee_out_idx.value == dec["fee_out_idx"]
    assert rdmr.protocol_cfg_ref_idx == dec["protocol_cfg_ref_idx"]
    assert rdmr.market_ref_idx == dec["market_ref_idx"]
    assert rdmr.pool_in_out_ref.transaction_id.hex() == dec["pool_in_out_ref"][0]
    assert rdmr.pool_in_out_ref.output_index == dec["pool_in_out_ref"][1]
    # The redeemer references the spent pool input's out-ref.
    assert rdmr.pool_in_out_ref.transaction_id.hex() == FULL["pool_in_out_ref"][0]
    assert rdmr.pool_in_out_ref.output_index == FULL["pool_in_out_ref"][1]


def test_partial_repay_decoded_fields_match_fixture():
    rdmr = DecreaseLoanAmount.from_cbor(
        bytes.fromhex(PARTIAL["decrease_loan_redeemer"])
    )
    dec = PARTIAL["decrease_loan_redeemer_decoded"]
    assert rdmr.CONSTR_ID == 5 == dec["constr"]
    assert rdmr.pool_out_idx == dec["pool_out_idx"]
    # Partial repay keeps a smaller loan UTxO: loan_out_idx points at it.
    assert isinstance(rdmr.loan_out_idx, SomeInt)
    assert rdmr.loan_out_idx.value == dec["loan_out_idx"]
    assert isinstance(rdmr.fee_out_idx, SomeInt)
    assert rdmr.fee_out_idx.value == dec["fee_out_idx"]
    assert rdmr.protocol_cfg_ref_idx == dec["protocol_cfg_ref_idx"]
    assert rdmr.market_ref_idx == dec["market_ref_idx"]
    assert rdmr.pool_in_out_ref.transaction_id.hex() == dec["pool_in_out_ref"][0]
    assert rdmr.pool_in_out_ref.output_index == dec["pool_in_out_ref"][1]
    assert rdmr.pool_in_out_ref.transaction_id.hex() == PARTIAL["pool_in_out_ref"][0]
    assert rdmr.pool_in_out_ref.output_index == PARTIAL["pool_in_out_ref"][1]


def test_full_repay_fresh_construct_matches_capture():
    dec = FULL["decrease_loan_redeemer_decoded"]
    rdmr = DecreaseLoanAmount(
        pool_out_idx=dec["pool_out_idx"],
        loan_out_idx=NoneVal(),
        fee_out_idx=SomeInt(dec["fee_out_idx"]),
        protocol_cfg_ref_idx=dec["protocol_cfg_ref_idx"],
        market_ref_idx=dec["market_ref_idx"],
        pool_in_out_ref=OutputReference(
            bytes.fromhex(dec["pool_in_out_ref"][0]),
            dec["pool_in_out_ref"][1],
        ),
    )
    assert rdmr.to_cbor().hex() == FULL["decrease_loan_redeemer"]


def test_partial_repay_fresh_construct_matches_capture():
    dec = PARTIAL["decrease_loan_redeemer_decoded"]
    rdmr = DecreaseLoanAmount(
        pool_out_idx=dec["pool_out_idx"],
        loan_out_idx=SomeInt(dec["loan_out_idx"]),
        fee_out_idx=SomeInt(dec["fee_out_idx"]),
        protocol_cfg_ref_idx=dec["protocol_cfg_ref_idx"],
        market_ref_idx=dec["market_ref_idx"],
        pool_in_out_ref=OutputReference(
            bytes.fromhex(dec["pool_in_out_ref"][0]),
            dec["pool_in_out_ref"][1],
        ),
    )
    assert rdmr.to_cbor().hex() == PARTIAL["decrease_loan_redeemer"]


def test_fresh_construct_round_trips():
    txid = bytes.fromhex(
        "1237d542cffbea900cf7828d2db98786caede1bab01870ec5e2f4754423d6be2"
    )
    full = DecreaseLoanAmount(
        pool_out_idx=0,
        loan_out_idx=NoneVal(),
        fee_out_idx=SomeInt(1),
        protocol_cfg_ref_idx=2,
        market_ref_idx=3,
        pool_in_out_ref=OutputReference(txid, 0),
    )
    partial = DecreaseLoanAmount(
        pool_out_idx=0,
        loan_out_idx=SomeInt(1),
        fee_out_idx=SomeInt(3),
        protocol_cfg_ref_idx=3,
        market_ref_idx=10,
        pool_in_out_ref=OutputReference(txid, 0),
    )
    assert DecreaseLoanAmount.from_cbor(full.to_cbor()).to_cbor() == full.to_cbor()
    assert (
        DecreaseLoanAmount.from_cbor(partial.to_cbor()).to_cbor() == partial.to_cbor()
    )
