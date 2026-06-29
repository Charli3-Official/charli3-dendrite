"""IncreaseLoanAmount redeemer must round-trip byte-exact against the captured tx.

An increase-loan reuses one redeemer instance across the pool ``Spend``, the loan
``Spend``, and the ``Withdraw(pool_skh)`` hub. ``loan_out_idx`` is a PLAIN ``int``
(the loan output is always present), and ``fee_out_idx`` is ``None`` on markets with
a zero origination-fee rate.
"""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.transactions.redeemers import IncreaseLoanAmount
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import OutputReference
from charli3_dendrite.lending.danogo.transactions.redeemers import SomeInt

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "increase_loan_tx.json").read_text()
)


def test_increase_loan_round_trips_byte_exact():
    cbor = FIX["increase_loan_redeemer"]
    rdmr = IncreaseLoanAmount.from_cbor(bytes.fromhex(cbor))
    assert rdmr.to_cbor().hex() == cbor


def test_increase_loan_redeemer_reused_across_roles():
    """The pool Spend, loan Spend, and Withdraw(pool_skh) hub share one instance."""
    tag = 125  # CBOR tag for the loan validator's alt index 4.
    shared = {
        r["cbor"]
        for r in FIX["redeemers"]
        if r["cbor"].startswith("d87d")
        and r["script_hash"] in (FIX["pool_skh"], FIX["loan_skh"])
    }
    assert len(shared) == 1
    assert shared.pop() == FIX["increase_loan_redeemer"]
    # All three carry tag 125 (alt index 4).
    constr = IncreaseLoanAmount.from_cbor(
        bytes.fromhex(FIX["increase_loan_redeemer"])
    ).CONSTR_ID
    assert 121 + constr == tag


def test_increase_loan_decoded_fields_match_fixture():
    rdmr = IncreaseLoanAmount.from_cbor(bytes.fromhex(FIX["increase_loan_redeemer"]))
    dec = FIX["increase_loan_redeemer_decoded"]
    assert rdmr.CONSTR_ID == 4 == dec["constr"]
    assert rdmr.pool_out_idx == dec["pool_out_idx"]
    # The loan output is always present on an increase: loan_out_idx is a plain int.
    assert isinstance(rdmr.loan_out_idx, int)
    assert rdmr.loan_out_idx == dec["loan_out_idx"]
    # Current mainnet markets charge no origination fee, so there is no fee output.
    assert isinstance(rdmr.fee_out_idx, NoneVal)
    assert dec["fee_out_idx"] is None
    assert rdmr.protocol_cfg_ref_idx == dec["protocol_cfg_ref_idx"]
    assert rdmr.market_ref_idx == dec["market_ref_idx"]
    assert rdmr.pool_in_out_ref.transaction_id.hex() == dec["pool_in_out_ref"][0]
    assert rdmr.pool_in_out_ref.output_index == dec["pool_in_out_ref"][1]
    # The redeemer references the spent pool input's out-ref.
    assert rdmr.pool_in_out_ref.transaction_id.hex() == FIX["pool_in_out_ref"][0]
    assert rdmr.pool_in_out_ref.output_index == FIX["pool_in_out_ref"][1]


def test_increase_loan_fresh_construct_matches_capture():
    dec = FIX["increase_loan_redeemer_decoded"]
    rdmr = IncreaseLoanAmount(
        pool_out_idx=dec["pool_out_idx"],
        loan_out_idx=dec["loan_out_idx"],
        fee_out_idx=NoneVal(),
        protocol_cfg_ref_idx=dec["protocol_cfg_ref_idx"],
        market_ref_idx=dec["market_ref_idx"],
        pool_in_out_ref=OutputReference(
            bytes.fromhex(dec["pool_in_out_ref"][0]),
            dec["pool_in_out_ref"][1],
        ),
    )
    assert rdmr.to_cbor().hex() == FIX["increase_loan_redeemer"]


def test_increase_loan_with_fee_output_round_trips():
    """A market with an origination fee would carry ``fee_out_idx = Some(idx)``."""
    txid = bytes.fromhex(
        "e65c965d9e5c0e52daf9003b5670bc617bf6a1af2d4742dadc69934dba493c1e"
    )
    rdmr = IncreaseLoanAmount(
        pool_out_idx=0,
        loan_out_idx=1,
        fee_out_idx=SomeInt(2),
        protocol_cfg_ref_idx=3,
        market_ref_idx=4,
        pool_in_out_ref=OutputReference(txid, 0),
    )
    assert IncreaseLoanAmount.from_cbor(rdmr.to_cbor()).to_cbor() == rdmr.to_cbor()
