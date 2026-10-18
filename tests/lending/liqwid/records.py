"""Captured Liqwid V2 mainnet records and Liqwid API values for the Liqwid tests.

``fixtures/records.json.gz`` and ``fixtures/api_snapshot.json`` are written together,
at one chain tip, by the dev-only ``fixtures/_capture_records.py``; nothing here
touches the network. See that script's docstring for the entry layout.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.oracles import LiqwidFeed
from charli3_dendrite.lending.liqwid.oracles import liqwid_price_map
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.markets import ParseContext
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.qtoken import receipt_prices
from tests.lending.records_common import record_info

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


def parsed_records(
    kind: str,
    *,
    live: bool | None = None,
) -> list[tuple[dict[str, Any], LiqwidRecord]]:
    """(entry, record) for every captured entry of ``kind`` that parses as genuine."""
    deployment = default_deployment()
    pairs = []
    for rec in records_of(kind, live=live):
        record = parse_utxo(record_info(rec), deployment)
        if record is not None:
            pairs.append((rec, record))
    return pairs


def live_feeds() -> list[LiqwidFeed]:
    """Every oracle output live at the snapshot that parses as a feed."""
    deployment = default_deployment()
    feeds = []
    for rec in records_of("oracle", live=True):
        feed = parse_feed(record_info(rec), deployment)
        if feed is not None:
            feeds.append(feed)
    return feeds


def live_price_map(markets: Iterable[LendingMarket] = ()) -> PriceMap:
    """Liqwid's lovelace prices at the snapshot, plus the qTokens of ``markets``.

    Each market's qToken is priced through its receipt rate (``receipt_prices``).
    """
    prices = liqwid_price_map(live_feeds())
    for price in receipt_prices(markets, prices).prices.values():
        prices.add(price)
    return prices


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
