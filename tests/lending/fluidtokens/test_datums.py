import json
from pathlib import Path

import cbor2
import pytest
from pycardano import IndefiniteList
from pycardano.exception import DeserializeException

from charli3_dendrite.lending.fluidtokens import datums as v3_datums
from charli3_dendrite.lending.fluidtokens.datums import (
    PoolDatum,
    LoanDatum,
    RequestDatum,
)
from charli3_dendrite.lending.plutus import StrictPlutusData

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())


def _roundtrip(cls, cbor_hex):
    parsed = cls.from_cbor(cbor_hex)
    assert parsed.to_cbor_hex() == cbor_hex
    return parsed


def test_pool_datum_roundtrip():
    d = _roundtrip(PoolDatum, FIX["pool"]["datum_cbor"])
    assert d.common_data.interest_rate == 400
    assert len(d.collateral_options) == 10


def test_loan_datum_roundtrip():
    d = _roundtrip(LoanDatum, FIX["loan"]["datum_cbor"])
    assert d.principal_amount == 3_500_000_000


def test_request_datum_roundtrip():
    d = _roundtrip(RequestDatum, FIX["request"]["datum_cbor"])
    assert d.max_principal == 35_000_000_000


def test_pool_datum_post_init_list_encoding():
    """Empty list fields stay plain (``80``); non-empty become ``IndefiniteList``."""
    parsed = PoolDatum.from_cbor(FIX["pool"]["datum_cbor"])
    assert isinstance(parsed.collateral_options, IndefiniteList)
    assert isinstance(parsed.min_collateral, IndefiniteList)
    assert isinstance(parsed.min_collateral_divider, IndefiniteList)

    empty = PoolDatum(
        permissioned_condition_script_hash=parsed.permissioned_condition_script_hash,
        extra_data=parsed.extra_data,
        common_data=parsed.common_data,
        lender_auth=parsed.lender_auth,
        lender_bond_address=parsed.lender_bond_address,
        lender_bond_inline_datum_hash=parsed.lender_bond_inline_datum_hash,
        collateral_options=[],
        min_collateral=[],
        min_collateral_divider=[],
        dynamic_collateral_price=parsed.dynamic_collateral_price,
    )
    assert not isinstance(empty.collateral_options, IndefiniteList)
    assert not isinstance(empty.min_collateral, IndefiniteList)
    assert not isinstance(empty.min_collateral_divider, IndefiniteList)
    assert empty.collateral_options == []
    # Three consecutive empty Plutus lists each encode as the definite-empty ``80``.
    assert "808080" in empty.to_cbor_hex()


V4_FIX = json.loads(
    (
        Path(__file__).parents[1] / "fluidtokens_v4" / "fixtures" / "entities.json"
    ).read_text(),
)


def test_every_v3_datum_class_is_strict():
    classes = [
        obj
        for obj in vars(v3_datums).values()
        if isinstance(obj, type) and obj.__module__ == v3_datums.__name__
    ]
    assert classes
    assert all(issubclass(cls, StrictPlutusData) for cls in classes)


def test_v3_pool_datum_rejects_every_v4_pool():
    # A V4 pool's CommonData carries one more field than V3's; the V3 class must not
    # decode it (it used to, silently dropping the new field).
    for rec in V4_FIX["pool"]:
        with pytest.raises(DeserializeException):
            PoolDatum.from_cbor(rec["datum_cbor"])


def test_v3_liquidation_rejects_the_v4_variant():
    loan = cbor2.loads(bytes.fromhex(V4_FIX["loan"][0]["datum_cbor"]))
    liquidation_v4 = loan.value[10]
    assert len(liquidation_v4.value) == 4
    with pytest.raises(DeserializeException):
        v3_datums.Liquidation.from_primitive(liquidation_v4)


def test_v3_pool_datum_rejects_a_surplus_top_level_field():
    top = cbor2.loads(bytes.fromhex(FIX["pool"]["datum_cbor"]))
    padded = cbor2.CBORTag(top.tag, [*top.value, b""])
    with pytest.raises(DeserializeException):
        PoolDatum.from_primitive(padded)


def test_tx_out_ref_round_trip():
    ref = v3_datums.TxOutRef(tx_id=bytes.fromhex("ab" * 32), index=3)
    assert v3_datums.TxOutRef.from_cbor(ref.to_cbor_hex()) == ref


def test_v3_loan_datum_rejects_every_v4_loan_with_ltv_liquidation():
    # A V4 Liquidation mode (constructor alternative 2, tag 123, loan field 10)
    # carries a fourth field; the typed V3 union must not decode it. The
    # NoLiquidation* modes keep the V3 layout, so V4 loans using them are left out.
    ltv_loans = [
        rec
        for rec in V4_FIX["loan"]
        if cbor2.loads(bytes.fromhex(rec["datum_cbor"])).value[10].tag == 123
    ]
    assert ltv_loans
    for rec in ltv_loans:
        with pytest.raises(DeserializeException):
            LoanDatum.from_cbor(rec["datum_cbor"])


def test_v3_liquidation_mode_decodes_typed():
    loan = LoanDatum.from_cbor(FIX["loan"]["datum_cbor"])
    assert isinstance(
        loan.liquidation_mode,
        (
            v3_datums.NoLiquidationFullCollateralClaim,
            v3_datums.NoLiquidationDutchAuctionClaim,
            v3_datums.Liquidation,
        ),
    )
