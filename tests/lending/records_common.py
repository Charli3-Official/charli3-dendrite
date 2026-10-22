"""Backend-shaped UTxO records for captured fixture entries, shared by the protocols.

A captured entry is ``{out_ref, address, datum_cbor, assets}``, optionally with the
``block_time`` and ``block_index`` of the transaction that created it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo

# Block of an entry captured without its chain position (POSIX seconds, tx index).
BLOCK_TIME = 1_790_000_000
BLOCK_INDEX = 0


def record_info(rec: Mapping[str, Any], **overrides: Any) -> PoolStateInfo:
    """A backend-shaped UTxO record for a captured entry (fields overridable)."""
    tx_hash, index = rec["out_ref"].split("#")
    values = {
        "address": rec["address"],
        "tx_hash": tx_hash,
        "tx_index": int(index),
        "block_time": rec.get("block_time", BLOCK_TIME),
        "block_index": rec.get("block_index", BLOCK_INDEX),
        "block_hash": "",
        "datum_hash": "",
        "datum_cbor": rec["datum_cbor"],
        "assets": Assets(root=dict(rec["assets"])),
        "plutus_v2": False,
    }
    values.update(overrides)
    return PoolStateInfo(**values)
