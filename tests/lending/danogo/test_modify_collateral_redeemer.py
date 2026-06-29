"""ModifyCollaterals redeemer must round-trip byte-exact against captured txs.

A modify-collateral tx adds and/or removes collateral on an existing loan without
repaying. The redeemer rides only the loan ``Spend`` (the pool is referenced, not
spent), is the loan validator's alt index 9 (CBOR tag 1282 / hex prefix ``d90502``),
and carries four plain ``int`` index fields and no ``pool_in_out_ref``. The
``d90502`` prefix assertion guards the alt-9 encoding (a prior d882-vs-d90502 tag bug
made an on-chain scan miss every one of these txs).
"""

import json
from pathlib import Path

import pytest

from charli3_dendrite.lending.danogo.transactions.redeemers import ModifyCollaterals

FIX_DIR = Path(__file__).parent / "fixtures"
FIXTURES = [
    "modify_collateral_add_tx.json",
    "modify_collateral_remove_tx.json",
    "modify_collateral_swap_tx.json",
]


def _load(name: str) -> dict:
    return json.loads((FIX_DIR / name).read_text())


@pytest.fixture(params=FIXTURES)
def fix(request) -> dict:
    return _load(request.param)


def test_modify_collateral_round_trips_byte_exact(fix):
    cbor = fix["modify_collateral_redeemer"]
    rdmr = ModifyCollaterals.from_cbor(bytes.fromhex(cbor))
    assert rdmr.to_cbor().hex() == cbor


def test_modify_collateral_cbor_prefix_is_d90502(fix):
    """The alt-9 encoding (tag 1282) must serialize with the ``d90502`` prefix."""
    cbor = fix["modify_collateral_redeemer"]
    assert cbor.startswith("d90502")
    assert ModifyCollaterals.from_cbor(bytes.fromhex(cbor)).to_cbor().hex()[:6] == (
        "d90502"
    )


def test_modify_collateral_is_alt_index_nine(fix):
    rdmr = ModifyCollaterals.from_cbor(bytes.fromhex(fix["modify_collateral_redeemer"]))
    assert rdmr.CONSTR_ID == 9 == fix["modify_collateral_redeemer_decoded"]["constr"]


def test_modify_collateral_decoded_fields_match_fixture(fix):
    rdmr = ModifyCollaterals.from_cbor(bytes.fromhex(fix["modify_collateral_redeemer"]))
    dec = fix["modify_collateral_redeemer_decoded"]
    # Four plain ints, no Option-wrapping and no pool_in_out_ref.
    assert isinstance(rdmr.loan_out_idx, int)
    assert isinstance(rdmr.protocol_cfg_ref_idx, int)
    assert isinstance(rdmr.market_ref_idx, int)
    assert isinstance(rdmr.pool_ref_idx, int)
    assert rdmr.loan_out_idx == dec["loan_out_idx"]
    assert rdmr.protocol_cfg_ref_idx == dec["protocol_cfg_ref_idx"]
    assert rdmr.market_ref_idx == dec["market_ref_idx"]
    assert rdmr.pool_ref_idx == dec["pool_ref_idx"]
    assert not hasattr(rdmr, "pool_in_out_ref")


def test_modify_collateral_fresh_construct_matches_capture(fix):
    dec = fix["modify_collateral_redeemer_decoded"]
    rdmr = ModifyCollaterals(
        loan_out_idx=dec["loan_out_idx"],
        protocol_cfg_ref_idx=dec["protocol_cfg_ref_idx"],
        market_ref_idx=dec["market_ref_idx"],
        pool_ref_idx=dec["pool_ref_idx"],
    )
    assert rdmr.to_cbor().hex() == fix["modify_collateral_redeemer"]


def test_modify_collateral_rides_loan_spend_only():
    """The redeemer is a loan Spend; no pool spend and no mint accompany it."""
    for name in FIXTURES:
        fix = _load(name)
        assert not fix["mints"], f"{name}: modify-collateral mints nothing"
        spends = [
            r
            for r in fix["redeemers"]
            if r["purpose"] == "spend" and r["cbor"].startswith("d90502")
        ]
        assert len(spends) == 1
        assert spends[0]["cbor"] == fix["modify_collateral_redeemer"]
        # The pool script is never spent (it is a reference input).
        assert not any(
            r["purpose"] == "spend" and r["script_hash"] == fix["pool_skh"]
            for r in fix["redeemers"]
        )
