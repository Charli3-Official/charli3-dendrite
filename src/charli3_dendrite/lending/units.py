"""Shared unit, credential and Plutus-constructor helpers for lending protocols."""
from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Protocol

import cbor2  # type: ignore[import-not-found]
from pycardano import Address
from pycardano import Network
from pycardano import RawPlutusData
from pycardano import ScriptHash
from pycardano.exception import DecodingException
from pycardano.exception import DeserializeException
from pycardano.exception import InvalidAddressInputException

if TYPE_CHECKING:
    from charli3_dendrite.dataclasses.models import Assets

# Hex length of a policy id (a 28-byte script hash): the policy prefix of a unit.
POLICY_HEX_LEN = 56

# Everything a malformed record can raise while its datum is decoded. Truncated CBOR
# raises cbor2's end-of-stream error, which is not a ValueError.
DECODE_ERRORS = (
    cbor2.CBORDecodeError,
    DeserializeException,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
)

# CBOR constructor tags map to Plutus alternative indices: tags 121..127 cover
# alts 0..6; tags 1280+ cover alts 7+ (alt = tag - 1280 + 7). The general form
# (tag 102) carries the alt inside its value rather than the tag, so it is not a
# pure tag->alt mapping and is rejected by `constr_alt`.
_CONSTR_TAG_MIN = 121
_CONSTR_TAG_MAX = 127
_CONSTR_TAG_EXT = 1280
_CONSTR_EXT_OFFSET = 7
_CONSTR_GENERAL_TAG = 102


def asset_unit(policy: bytes, name: bytes) -> str:
    """Dendrite unit string for a (policy, name) asset ('lovelace' for ADA)."""
    if not policy and not name:
        return "lovelace"
    return policy.hex() + name.hex()


def policy_of(unit: str) -> str:
    """Policy id (hex) of a dendrite unit; ``""`` for lovelace."""
    return "" if unit == "lovelace" else unit[:POLICY_HEX_LEN]


def canonical_unit_key(unit: str) -> str:
    """Sort key of the canonical unit order: lovelace first, then policy id + name."""
    return "" if unit == "lovelace" else unit


def names_under(assets: Assets, policy: str) -> list[str]:
    """Asset names (hex, sorted) held under ``policy``."""
    return sorted(
        unit[POLICY_HEX_LEN:]
        for unit in assets.root
        if unit != "lovelace" and unit[:POLICY_HEX_LEN] == policy
    )


def sole_token_name(assets: Assets, policy: str) -> str | None:
    """Asset name (hex) of the one token held under ``policy``, if there is one.

    ``assets`` must hold exactly one token under ``policy``, with quantity 1; its name
    may be empty. Anything else returns None.
    """
    names = names_under(assets, policy)
    if len(names) != 1 or assets.root[policy + names[0]] != 1:
        return None
    return names[0]


class OutRefSource(Protocol):
    """Anything that names a UTxO by transaction hash and output index."""

    tx_hash: str
    tx_index: int


def out_ref(info: OutRefSource) -> str:
    """``tx_hash#tx_index`` of a UTxO."""
    return f"{info.tx_hash}#{info.tx_index}"


def out_ref_key(ref: str) -> tuple[str, int]:
    """``(tx_hash, index)`` of an out-ref, the index compared as an integer.

    An index that is not a non-negative integer reads as -1.
    """
    tx_hash, _, index = ref.partition("#")
    return (tx_hash, int(index) if index.isdigit() else -1)


def plutus_fields(obj: object) -> list:
    """Positional fields of a compact-form CBOR Constr (or a bare list) as a list.

    Any ``cbor2.CBORTag`` is unwrapped without checking its tag, so this suits the
    compact constructor tags (121-127, 1280+) and bare lists. A general-form
    constructor (tag 102) wraps ``[alt, fields]`` and would come back as that pair;
    use ``constr`` for it.
    """
    inner = obj.value if isinstance(obj, cbor2.CBORTag) else obj
    return list(inner)


def tuple_asset_unit(arr: object) -> str:
    """Dendrite unit string from a ``[policy, name]`` value (Constr-wrapped or bare)."""
    seq = plutus_fields(arr)
    policy = seq[0] if len(seq) > 0 and isinstance(seq[0], bytes) else b""
    name = seq[1] if len(seq) > 1 and isinstance(seq[1], bytes) else b""
    return asset_unit(policy, name)


def script_payment_address(script_hash_hex: str) -> str:
    """Mainnet enterprise address whose payment credential is ``script_hash_hex``.

    Protocol UTxOs sit at base addresses whose stake part varies per user, but backends
    discover UTxOs by payment credential only, so this address is a stable selector.
    """
    return Address(
        payment_part=ScriptHash(bytes.fromhex(script_hash_hex)),
        network=Network.MAINNET,
    ).encode()


def script_credential(address: str) -> str | None:
    """Payment script hash (hex) of a bech32 address; None for any other address.

    Protocol entities sit at script credentials, so a key-hash payment part (even one
    whose bytes equal a protocol script hash) returns None, as does an unparseable
    address. pycardano asserts key-hash sizes and decodes a bech32 Byron header as
    CBOR, so a malformed address can also raise ``AssertionError`` or
    ``DeserializeException``; both read as None.
    """
    try:
        part = Address.decode(address).payment_part
    except (
        AssertionError,
        DecodingException,
        DeserializeException,
        InvalidAddressInputException,
        TypeError,
        ValueError,
    ):
        return None
    return part.payload.hex() if isinstance(part, ScriptHash) else None


def constr_alt(tag: int) -> int:
    """CBOR constructor tag -> Plutus alternative index (pycardano convention)."""
    if _CONSTR_TAG_MIN <= tag <= _CONSTR_TAG_MAX:
        return tag - _CONSTR_TAG_MIN
    if tag >= _CONSTR_TAG_EXT:
        return tag - _CONSTR_TAG_EXT + _CONSTR_EXT_OFFSET
    raise ValueError(f"not a constructor tag: {tag}")


def constr(raw: object) -> tuple[int, list]:
    """(alternative index, positional fields) of a decoded Plutus Constr.

    Accepts a ``RawPlutusData`` (union datum field), a ``cbor2.CBORTag``, or a typed
    ``PlutusData`` (exposing ``CONSTR_ID`` + fields as attributes). Raises
    ``ValueError`` for a CBOR tag outside the known constructor ranges.
    """
    if isinstance(raw, RawPlutusData):
        raw = raw.data
    if isinstance(raw, cbor2.CBORTag):
        tag, value = raw.tag, raw.value
        if tag == _CONSTR_GENERAL_TAG:
            return int(value[0]), list(value[1])
        return constr_alt(tag), list(value)
    return getattr(raw, "CONSTR_ID", 0), list(getattr(raw, "__dict__", {}).values())
