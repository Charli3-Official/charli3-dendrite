"""Captured Liqwid V2 mainnet records and Liqwid API values for the Liqwid tests.

``fixtures/records.json.gz`` and ``fixtures/api_snapshot.json`` are written together,
at one chain tip, by the dev-only ``fixtures/_capture_records.py``; nothing here
touches the network. See that script's docstring for the entry layout.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.markets import ParseContext

FIXTURES = Path(__file__).parent / "fixtures"
RECORDS: dict[str, Any] = json.loads(
    gzip.decompress((FIXTURES / "records.json.gz").read_bytes()),
)
API: dict[str, Any] = json.loads((FIXTURES / "api_snapshot.json").read_text())

UTXO_KINDS: tuple[str, ...] = (
    "market_state",
    "loan",
    "market_params",
    "collateral_params",
    "oracle",
)
CONTEXT_KINDS: tuple[str, ...] = (
    "market_params",
    "collateral_params",
    "market_state",
    "oracle",
)
REJECT = "reject"
# Spend fixture key -> ``classify_spend`` rule (1-8) it exercises; None = the
# state-match pass of ``match_successors``.
SPEND_RULES: dict[str, int | None] = {
    "partial_liquidation": 1,
    "new_modification": 2,
    "full_repay": 2,
    "legacy_liquidation": 3,
    "legacy_repay": 4,
    "legacy_flip": 5,
    "admin_migration": 6,
    "admin_reown": 6,
    "parking": 7,
    "same_generation_rewrite": 8,
    "multi_loan": None,
}


def record_info(rec: Mapping[str, Any], **overrides: Any) -> PoolStateInfo:
    """A backend-shaped UTxO record for a captured entry (fields overridable)."""
    tx_hash, index = rec["out_ref"].split("#")
    values = {
        "address": rec["address"],
        "tx_hash": tx_hash,
        "tx_index": int(index),
        "block_time": rec["block_time"],
        "block_index": rec["block_index"],
        "block_hash": "",
        "datum_hash": "",
        "datum_cbor": rec["datum_cbor"],
        "assets": Assets(root=dict(rec["assets"])),
        "plutus_v2": False,
    }
    values.update(overrides)
    return PoolStateInfo(**values)


def records_of(kind: str, *, live: bool | None = None) -> list[dict[str, Any]]:
    """Captured entries of ``kind``, optionally only those (not) live at the snapshot.

    A UTxO kind holds its genuine records and the captured non-records found at its
    credentials (``label`` set); ``"reject"`` returns every labelled non-record of
    every UTxO kind. ``spends`` and ``max_borrows`` entries carry no ``live`` flag.
    """
    if kind == REJECT:
        entries = [
            rec for k in UTXO_KINDS for rec in RECORDS[k] if rec["label"] is not None
        ]
    else:
        entries = list(RECORDS[kind])
    if live is None:
        return entries
    return [rec for rec in entries if rec["live"] is live]


def live_context() -> ParseContext:
    """Every live parameter, MarketState and oracle UTxO, at the snapshot slot."""
    utxos = tuple(
        record_info(rec)
        for kind in CONTEXT_KINDS
        for rec in records_of(kind, live=True)
    )
    return ParseContext(slot=API["slot"], utxos=utxos)


def context_at(slot: int) -> ParseContext:
    """Captured parameter, MarketState and oracle UTxOs existing just before ``slot``.

    An entry counts when it was created before ``slot`` and was unspent or spent at
    ``slot`` or later: an output spent at ``slot`` still existed just before it. For
    the context of a record created at slot ``s`` use ``context_at(s + 1)``.
    """
    utxos = tuple(
        record_info(rec)
        for kind in CONTEXT_KINDS
        for rec in RECORDS[kind]
        if rec["slot"] < slot
        and (rec["spent_slot"] is None or rec["spent_slot"] >= slot)
    )
    return ParseContext(slot=slot, utxos=utxos)


def spend_of(rule: str) -> dict[str, Any]:
    """The captured spend for ``rule`` (a ``SPEND_RULES`` key); KeyError otherwise."""
    matches = [spend for spend in RECORDS["spends"] if spend["rule"] == rule]
    if len(matches) != 1:
        raise KeyError(rule)
    return matches[0]
