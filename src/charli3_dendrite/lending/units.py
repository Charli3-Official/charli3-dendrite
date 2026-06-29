"""Shared asset-unit and Plutus-constructor helpers for the lending protocols."""
from __future__ import annotations

# CBOR constructor tags map to Plutus alternative indices: tags 121..127 cover
# alts 0..6; tags 1280+ cover alts 7+ (alt = tag - 1280 + 7). The general form
# (tag 102) carries the alt inside its value rather than the tag, so it is not a
# pure tag->alt mapping and is rejected here.
_CONSTR_TAG_MIN = 121
_CONSTR_TAG_MAX = 127
_CONSTR_TAG_EXT = 1280
_CONSTR_EXT_OFFSET = 7


def asset_unit(policy: bytes, name: bytes) -> str:
    """Dendrite unit string for a (policy, name) asset ('lovelace' for ADA)."""
    if not policy and not name:
        return "lovelace"
    return policy.hex() + name.hex()


def constr_alt(tag: int) -> int:
    """CBOR constructor tag -> Plutus alternative index (pycardano convention)."""
    if _CONSTR_TAG_MIN <= tag <= _CONSTR_TAG_MAX:
        return tag - _CONSTR_TAG_MIN
    if tag >= _CONSTR_TAG_EXT:
        return tag - _CONSTR_TAG_EXT + _CONSTR_EXT_OFFSET
    raise ValueError(f"not a constructor tag: {tag}")
