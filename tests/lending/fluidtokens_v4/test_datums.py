"""Byte-exact decode / encode of every live FluidTokens V4 datum kind."""

import dataclasses
import json
from pathlib import Path

import cbor2
import pytest
from pycardano import IndefiniteList
from pycardano import RawPlutusData
from pycardano.exception import DeserializeException

from charli3_dendrite.lending.fluidtokens import datums as v3
from charli3_dendrite.lending.fluidtokens_v4 import datums as v4

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())
V3_FIX = json.loads(
    (
        Path(__file__).parents[1] / "fluidtokens" / "fixtures" / "entities.json"
    ).read_text(),
)

BYTE_EXACT = [
    ("config", v4.ConfigDatum.from_cbor),
    ("pool", v4.PoolDatum.from_cbor),
    ("pool_manager", v4.PoolManagerDatum.from_cbor),
    ("loan", v4.LoanDatum.from_cbor),
    ("asset_manager", v4.decode_asset_manager_datum),
    ("lender_manager", v4.LenderManagerDatum.from_cbor),
]


@pytest.mark.parametrize(("kind", "decode"), BYTE_EXACT)
def test_every_live_datum_round_trips_byte_exact(kind, decode):
    records = FIX[kind]
    assert records, f"fixture has no {kind} records"
    for rec in records:
        assert decode(rec["datum_cbor"]).to_cbor_hex() == rec["datum_cbor"], rec[
            "out_ref"
        ]


def test_lender_manager_config_round_trips_byte_exact():
    rec = FIX["lender_manager_config"]
    decoded = v4.LenderManagerConfigDatum.from_cbor(rec["datum_cbor"])
    assert decoded.to_cbor_hex() == rec["datum_cbor"]


def test_pool_decodes_typed_v4_terms():
    pool = v4.PoolDatum.from_cbor(FIX["pool"][0]["datum_cbor"])
    assert isinstance(pool.common_data, v4.CommonData)
    assert isinstance(pool.common_data.liquidation_mode, v4.Liquidation)
    assert pool.common_data.borrower_bond_destination_script_hash == b""
    assert isinstance(pool.collateral_options, IndefiniteList)


def test_loan_decodes_typed_v4_liquidation():
    loan = v4.LoanDatum.from_cbor(FIX["loan"][0]["datum_cbor"])
    liquidation = loan.liquidation_mode
    assert isinstance(liquidation, v4.Liquidation)
    assert liquidation.l_tv_divider > 0
    assert isinstance(liquidation.equity_in_principal_currency, RawPlutusData)


def test_asset_manager_decodes_to_token_variant():
    decoded = v4.decode_asset_manager_datum(FIX["asset_manager"][0]["datum_cbor"])
    assert isinstance(decoded, v4.AssetManagerDatumWithToken)
    assert isinstance(decoded.input_output_reference, v4.TxOutRef)


def test_asset_manager_hash_variant_round_trips():
    with_hash = v4.AssetManagerDatumWithHash(
        input_output_reference=v4.TxOutRef(tx_id=b"\x01" * 32, index=0),
        action=b"installment_repayment",
        data=RawPlutusData(cbor2.CBORTag(121, [])),
        owner_auth=v4.AuthCardanoSignature(key_hash=b"\x02" * 28),
    )
    decoded = v4.decode_asset_manager_datum(with_hash.to_cbor_hex())
    assert isinstance(decoded, v4.AssetManagerDatumWithHash)
    assert decoded.to_cbor_hex() == with_hash.to_cbor_hex()


def test_asset_manager_decoder_rejects_other_datums():
    with pytest.raises(DeserializeException):
        v4.decode_asset_manager_datum(FIX["pool_manager"][0]["datum_cbor"])


def test_locked_borrower_manager_round_trips():
    locked = v4.LockedBorrowerManagerDatum(
        origin_ref=v4.TxOutRef(tx_id=b"\x03" * 32, index=2),
        borrower_auth=v4.AuthCardanoSignature(key_hash=b"\x04" * 28),
    )
    decoded = v4.LockedBorrowerManagerDatum.from_cbor(locked.to_cbor_hex())
    assert decoded.to_cbor_hex() == locked.to_cbor_hex()


@pytest.mark.parametrize(
    "mode",
    [v4.NoLiquidationFullCollateralClaim(), v4.NoLiquidationDutchAuctionClaim()],
)
def test_other_liquidation_variants_round_trip(mode):
    loan = v4.LoanDatum.from_cbor(FIX["loan"][0]["datum_cbor"])
    loan.liquidation_mode = mode
    decoded = v4.LoanDatum.from_cbor(loan.to_cbor_hex())
    assert type(decoded.liquidation_mode) is type(mode)
    assert decoded.to_cbor_hex() == loan.to_cbor_hex()


def test_request_datum_round_trips_with_v4_common_data():
    # The fixture has no V4 request; build one from the V3 request with V4 terms.
    v3_request = v3.RequestDatum.from_cbor(V3_FIX["request"]["datum_cbor"])
    pool = v4.PoolDatum.from_cbor(FIX["pool"][0]["datum_cbor"])
    v3_request.common_data = pool.common_data
    request = v4.RequestDatum(
        **{
            f.name: getattr(v3_request, f.name)
            for f in dataclasses.fields(v3.RequestDatum)
        }
    )
    decoded = v4.RequestDatum.from_cbor(request.to_cbor_hex())
    assert isinstance(decoded.common_data, v4.CommonData)
    assert decoded.to_cbor_hex() == request.to_cbor_hex()


@pytest.mark.parametrize(
    ("v4_cls", "v3_cls"),
    [
        (v4.PoolDatum, v3.PoolDatum),
        (v4.RequestDatum, v3.RequestDatum),
        (v4.LoanDatum, v3.LoanDatum),
    ],
)
def test_reannotated_classes_keep_the_v3_field_order(v4_cls, v3_cls):
    names = [f.name for f in dataclasses.fields(v4_cls)]
    assert names == [f.name for f in dataclasses.fields(v3_cls)]


def test_common_data_appends_one_field():
    names = [f.name for f in dataclasses.fields(v4.CommonData)]
    assert names[:-1] == [f.name for f in dataclasses.fields(v3.CommonData)]
    assert names[-1] == "borrower_bond_destination_script_hash"


def test_liquidation_appends_one_field():
    names = [f.name for f in dataclasses.fields(v4.Liquidation)]
    assert names == [
        "l_tv",
        "l_tv_divider",
        "partial_liquidation_penalty_per_mille",
        "equity_in_principal_currency",
    ]


@pytest.mark.parametrize(
    ("kind", "v4_cls"),
    [
        ("pool", v4.PoolDatum),
        ("loan", v4.LoanDatum),
        ("request", v4.RequestDatum),
    ],
)
def test_v4_classes_reject_v3_datums(kind, v4_cls):
    with pytest.raises(DeserializeException):
        v4_cls.from_cbor(V3_FIX[kind]["datum_cbor"])


def test_config_rejects_a_truncated_datum():
    top = cbor2.loads(bytes.fromhex(FIX["config"][-1]["datum_cbor"]))
    truncated = cbor2.CBORTag(top.tag, list(top.value)[:16])
    with pytest.raises(DeserializeException):
        v4.ConfigDatum.from_primitive(truncated)
