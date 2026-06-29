"""OraclePriceCalcRdmr: on-chain model (verified) + documented packed notation."""

import json
from pathlib import Path

import pytest

from charli3_dendrite.lending.danogo.oracles.redeemer import CalcType
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.oracles.redeemer import decode_oracle_idxs
from charli3_dendrite.lending.danogo.oracles.redeemer import decode_oracle_path_idxs
from charli3_dendrite.lending.danogo.oracles.redeemer import encode_oracle_idxs

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mainnet_datums.json").read_text()
)
REDEEMERS = {r["tx"]: r["redeemer"] for r in FIXTURES["oracle_redeemers"]}


# --- on-chain redeemer model (verified against live mainnet CBOR) ---------------


def test_decode_real_redeemer_single_idx():
    tx = "6b2bf2891b92ce72a7978c7f269f92359d967c6844d71275654a9e429a194491"
    r = OraclePriceCalcRdmr.from_cbor(REDEEMERS[tx])
    assert r.oracle_source_idx == 6
    assert r.oracle_path_idxs == [2]
    assert r.oracle_idxs == [(UTxOTarget.REF, OracleUtxoType.TDANOGO_POOL, 3)]
    # quote=USDCx, one collateral, exact PRational rate.
    quote = "1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e34" "5553444378"
    assert quote in r.prices
    collat = (
        "73f29518da0013a671458d52624a4828c5b5bedaff8a950b0063b1cf"
        "6098d885971a7a233bee756f2933a7a300bc49770ca66c11d6b31063"
    )
    assert r.prices[quote][collat] == (373929758169, 373651360855)
    assert r.borrow_rates == {}


def test_decode_real_redeemer_multi_idx_and_ada_price():
    tx = "929a7d68e0f05db06677c8bb675cd36be6a5d5fb23b5ddd008d5af4952de8237"
    r = OraclePriceCalcRdmr.from_cbor(REDEEMERS[tx])
    assert r.oracle_source_idx == 10
    assert r.oracle_path_idxs == [1]
    assert r.oracle_idxs == [
        (UTxOTarget.REF, OracleUtxoType.TLIQWID_ORACLE_V2, 0),
        (UTxOTarget.REF, OracleUtxoType.TDANOGO_POOL, 2),
        (UTxOTarget.REF, OracleUtxoType.TDJED, 12),
        (UTxOTarget.REF, OracleUtxoType.TMINSWAP_LP, 3),
        (UTxOTarget.OUT, OracleUtxoType.TDANOGO_POOL, 1),
    ]
    quote = (
        "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad" "0014df105553444d"
    )
    # ADA is priced as the bare (empty, empty) TupleAsset -> "lovelace".
    assert r.prices[quote]["lovelace"] == (16271, 100000)
    # bignum PRational also decodes to plain ints.
    assert len(r.prices[quote]) == 3


def test_all_fixture_redeemers_decode_to_known_enums():
    for cbor in REDEEMERS.values():
        r = OraclePriceCalcRdmr.from_cbor(cbor)
        for target, otype, idx in r.oracle_idxs:
            assert isinstance(target, UTxOTarget)
            assert isinstance(otype, OracleUtxoType)
            assert idx >= 0


# --- documented packed notation (integration guide; NOT the on-chain encoding) --


def test_decode_oracle_idxs_matches_docs_example():
    # From Danogo's "Create a Flexible Loan Transaction" guide:
    # 010800020d01 == [(Out, TDanogoStaking, 0), (In, TSplashCpammG3, 1)]
    decoded = decode_oracle_idxs("010800020d01")
    assert decoded == [
        (UTxOTarget.OUT, OracleUtxoType.TDANOGO_STAKING, 0),
        (UTxOTarget.IN, OracleUtxoType.TSPLASH_CPAMM_G3, 1),
    ]


def test_oracle_idxs_round_trip():
    raw = "010800020d01"
    assert encode_oracle_idxs(decode_oracle_idxs(raw)).hex() == raw


def test_oracle_path_idxs_single_byte_indices():
    assert decode_oracle_path_idxs("05") == [5]
    assert decode_oracle_path_idxs(b"\x00\x03\x05") == [0, 3, 5]


def test_enum_indices_are_stable():
    assert OracleUtxoType.TORCFAX_FSP == 0
    assert OracleUtxoType.TDANOGO_STAKING == 8
    assert OracleUtxoType.TSPLASH_CPAMM_G3 == 13
    assert OracleUtxoType.TCHARLI3 == 15
    assert OracleUtxoType.TMINSWAP_LP_STABLE == 16
    assert (UTxOTarget.REF, UTxOTarget.OUT, UTxOTarget.IN) == (0, 1, 2)
    assert (CalcType.NORMAL, CalcType.SPLASH) == (0, 1)


def test_decode_rejects_bad_length():
    with pytest.raises(ValueError):
        decode_oracle_idxs("0108")
