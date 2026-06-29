"""Shared asset-unit helpers for the lending protocols."""
from __future__ import annotations


def asset_unit(policy: bytes, name: bytes) -> str:
    """Dendrite unit string for a (policy, name) asset ('lovelace' for ADA)."""
    if not policy and not name:
        return "lovelace"
    return policy.hex() + name.hex()
