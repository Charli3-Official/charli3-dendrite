"""TopupWithdraw redeemer must round-trip byte-exact against the captured txs."""

import json
from pathlib import Path

from pycardano import IndefiniteList

from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import PoolMarketIndexer
from charli3_dendrite.lending.danogo.transactions.redeemers import SomeInt
from charli3_dendrite.lending.danogo.transactions.redeemers import TopupWithdraw

FIX_DIR = Path(__file__).parent / "fixtures"


def _spend_rdmr_cbor(fixture: str) -> str:
    fix = json.loads((FIX_DIR / fixture).read_text())
    return next(
        r["cbor"]
        for r in fix["redeemers"]
        if r["purpose"] == "spend" and r["script_hash"] == fix["pool_skh"]
    )


def test_topup_redeemer_roundtrips():
    cbor = _spend_rdmr_cbor("topup_tx.json")
    rdmr = TopupWithdraw.from_cbor(bytes.fromhex(cbor))
    assert rdmr.to_cbor().hex() == cbor


def test_withdraw_redeemer_roundtrips():
    cbor = _spend_rdmr_cbor("withdraw_tx.json")
    rdmr = TopupWithdraw.from_cbor(bytes.fromhex(cbor))
    assert rdmr.to_cbor().hex() == cbor


def test_topup_alt_redeemer_roundtrips():
    cbor = _spend_rdmr_cbor("topup_alt_tx.json")
    rdmr = TopupWithdraw.from_cbor(bytes.fromhex(cbor))
    assert rdmr.to_cbor().hex() == cbor


def test_single_pool_fresh_construct_matches_capture():
    # Building from scratch (not decoding) must reproduce the captured spend
    # redeemer -- this is the path the builder code (Tasks 7/8) relies on.
    rdmr = TopupWithdraw(
        protocol_cfg_ref_idx=2,
        pools=IndefiniteList(
            [PoolMarketIndexer(pool_out_idx=0, fee_out_idx=NoneVal(), market_ref_idx=0)]
        ),
    )
    assert rdmr.to_cbor().hex() == "d87b9f029fd8799f00d87a8000ffffff"


def test_multi_pool_fresh_construct_is_indefinite_and_roundtrips():
    p0 = PoolMarketIndexer(pool_out_idx=0, fee_out_idx=NoneVal(), market_ref_idx=1)
    p1 = PoolMarketIndexer(pool_out_idx=1, fee_out_idx=SomeInt(5), market_ref_idx=2)
    rdmr = TopupWithdraw(protocol_cfg_ref_idx=2, pools=IndefiniteList([p0, p1]))
    out = rdmr.to_cbor().hex()

    # Each nested entry serializes in the indefinite constr form d8799f...ff.
    assert p0.to_cbor().hex() == "d8799f00d87a8001ff"
    assert p1.to_cbor().hex() == "d8799f01d8799f05ff02ff"
    assert p0.to_cbor().hex() in out
    assert p1.to_cbor().hex() in out

    # Outer redeemer: tag 123 (d87b) + indefinite list (9f...ff) holding
    # protocol_cfg_ref_idx then the indefinite pools list (9f...ff).
    expected = (
        "d87b9f02"  # CONSTR_ID=2, outer indefinite-list header, idx=2
        + "9f"  # pools indefinite-list header
        + p0.to_cbor().hex()
        + p1.to_cbor().hex()
        + "ff"  # close pools
        + "ff"  # close outer
    )
    assert out == expected

    # Fresh-construct -> decode -> re-encode is byte-stable.
    assert TopupWithdraw.from_cbor(rdmr.to_cbor()).to_cbor() == rdmr.to_cbor()
