"""Plutus-data base classes shared by the lending protocols."""

from __future__ import annotations

from dataclasses import fields
from typing import Any
from typing import TypeVar

import cbor2  # type: ignore[import-not-found]
from pycardano import PlutusData
from pycardano.exception import DeserializeException

from charli3_dendrite.lending.units import constr_alt

_T = TypeVar("_T", bound="StrictPlutusData")

# The general constructor form (tag 102) carries ``[alternative, fields]``.
_GENERAL_FORM_TAG = 102
_GENERAL_FORM_ARITY = 2


def constr_field_count(value: object) -> int | None:
    """Field count of a CBOR-decoded Plutus constructor, or None for other values."""
    if not isinstance(value, cbor2.CBORTag):
        return None
    if value.tag == _GENERAL_FORM_TAG:
        payload = value.value
        if (
            not isinstance(payload, (list, tuple))
            or len(payload) != _GENERAL_FORM_ARITY
        ):
            return None
        constr_fields = payload[1]
    else:
        try:
            constr_alt(value.tag)
        except ValueError:
            return None
        constr_fields = value.value
    try:
        return len(constr_fields)
    except TypeError:
        return None


class StrictPlutusData(PlutusData):
    """``PlutusData`` whose decoder requires the exact constructor field count.

    pycardano's decoder zips a constructor's fields against the dataclass fields.
    Surplus fields are dropped (kept only as ``unknown_field*`` attributes, so the
    datum no longer re-encodes to the same bytes) and missing fields surface as a
    ``TypeError`` from the dataclass constructor. Either way the datum has a different
    layout from the class, so this decoder raises :class:`DeserializeException`
    instead. Raising that type keeps union dispatch working: pycardano tries each
    member of a ``Union`` field and moves on only when a member raises
    ``DeserializeException``.
    """

    @classmethod
    def from_primitive(
        cls: type[_T],
        value: Any,  # noqa: ANN401
    ) -> _T:  # noqa: PYI019
        """Decode ``value``, rejecting a constructor with the wrong field count."""
        count = constr_field_count(value)
        expected = sum(1 for f in fields(cls) if f.init)
        if count is not None and count != expected:
            raise DeserializeException(
                f"{cls.__name__}: expected {expected} constructor fields, got {count}",
            )
        return super().from_primitive(value)  # type: ignore[misc]
