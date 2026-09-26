"""Strict constructor-arity decoding for lending Plutus datums."""

from dataclasses import dataclass
from typing import Union

import cbor2
import pytest
from pycardano import PlutusData
from pycardano.exception import DeserializeException

from charli3_dendrite.lending.plutus import StrictPlutusData
from charli3_dendrite.lending.plutus import constr_field_count


@dataclass
class _Pair(StrictPlutusData):
    CONSTR_ID = 0
    a: int
    b: bytes


@dataclass
class _Unit(StrictPlutusData):
    CONSTR_ID = 1


@dataclass
class _Holder(StrictPlutusData):
    CONSTR_ID = 0
    inner: Union[_Unit, _Pair]


@dataclass
class _LenientPair(PlutusData):
    CONSTR_ID = 0
    a: int
    b: bytes


def test_exact_arity_round_trips():
    cbor_hex = _Pair(a=7, b=b"\x01").to_cbor_hex()
    assert _Pair.from_cbor(cbor_hex).to_cbor_hex() == cbor_hex


def test_surplus_field_raises():
    extra = cbor2.CBORTag(121, [7, b"\x01", b"surplus"])
    with pytest.raises(
        DeserializeException, match="expected 2 constructor fields, got 3"
    ):
        _Pair.from_primitive(extra)


def test_missing_field_raises_deserialize_not_type_error():
    short = cbor2.CBORTag(121, [7])
    with pytest.raises(
        DeserializeException, match="expected 2 constructor fields, got 1"
    ):
        _Pair.from_primitive(short)


def test_lenient_plutus_data_silently_drops_surplus_field():
    # The failure mode StrictPlutusData exists to prevent.
    extra = cbor2.CBORTag(121, [7, b"\x01", b"surplus"])
    decoded = _LenientPair.from_primitive(extra)
    assert decoded.to_primitive() != extra


def test_general_form_tag_102_is_checked():
    general = cbor2.CBORTag(102, [0, [7, b"\x01", b"surplus"]])
    with pytest.raises(DeserializeException):
        _Pair.from_primitive(general)


def test_zero_field_constructor():
    assert _Unit.from_primitive(cbor2.CBORTag(122, [])) == _Unit()
    with pytest.raises(DeserializeException):
        _Unit.from_primitive(cbor2.CBORTag(122, [1]))


def test_union_dispatch_skips_member_with_wrong_arity():
    # A 3-field alt-1 value matches _Unit's constructor tag but not its arity; the
    # strict _Unit must reject it so the union reports no match instead of binding it.
    holder = cbor2.CBORTag(121, [cbor2.CBORTag(122, [1, 2, 3])])
    with pytest.raises(DeserializeException):
        _Holder.from_primitive(holder)
    ok = cbor2.CBORTag(121, [cbor2.CBORTag(121, [5, b"\x02"])])
    assert _Holder.from_primitive(ok).inner == _Pair(a=5, b=b"\x02")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (cbor2.CBORTag(121, [1, 2]), 2),
        (cbor2.CBORTag(127, []), 0),
        (cbor2.CBORTag(1280, [1]), 1),
        (cbor2.CBORTag(102, [9, [1, 2, 3]]), 3),
        (cbor2.CBORTag(2, b"\x01\x02"), None),
        (cbor2.CBORTag(102, "junk"), None),
        ([1, 2], None),
        (5, None),
    ],
)
def test_constr_field_count(value, expected):
    assert constr_field_count(value) == expected
