import json
from pathlib import Path

from pycardano import IndefiniteList

from charli3_dendrite.lending.fluidtokens.datums import (
    PoolDatum,
    LoanDatum,
    RequestDatum,
)

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
