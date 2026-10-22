"""Backend-driven discovery of Liqwid V2 UTxOs.

A convenience over :func:`~charli3_dendrite.lending.liqwid.indexing.parse_utxo`: fetch
every unspent UTxO at each credential the deployment names, classify it, and gather
the live parse context (market parameters, collateral parameters, oracle feeds and
market states) from the same fetch. Prices are an input; nothing here fetches them.

Selectors are enterprise addresses matched by payment credential, which assumes a
backend that selects by payment credential (as the dbsync backend does); on a backend
that matches exact addresses the snapshot comes back empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from charli3_dendrite.lending.fetch import PAGE_SIZE
from charli3_dendrite.lending.fetch import resolve_now_ms
from charli3_dendrite.lending.fetch import unspent_utxos
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.markets import ParseContext

if TYPE_CHECKING:
    from collections.abc import Sequence

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
    from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
    from charli3_dendrite.lending.liqwid.indexing import LiqwidSelector


def fetch_records(
    backend: AbstractBackend,
    selectors: Sequence[LiqwidSelector],
    *,
    page_size: int = PAGE_SIZE,
    deployment: LiqwidDeployment | None = None,
) -> list[LiqwidRecord]:
    """Every unspent UTxO at the selectors' addresses that is a selected record.

    Each distinct address is fetched once, however many selectors share it (the
    parameter credential holds every market's MarketParams and CollateralParams). A
    UTxO is kept when ``parse_utxo`` classifies it as the kind and market of one of
    ``selectors``; anything else at the address (dust, decoys, a parked output) is
    dropped. ``deployment`` (the bundled mainnet one by default) is the one the
    selectors came from. Sorted by out-ref.
    """
    wanted = {(selector.kind, selector.market_id) for selector in selectors}
    found: dict[str, LiqwidRecord] = {}
    for address in sorted({selector.address for selector in selectors}):
        for info in unspent_utxos(backend, address, page_size=page_size):
            record = parse_utxo(info, deployment)
            if record is not None and (record.kind, record.market_id) in wanted:
                found.setdefault(record.out_ref, record)
    return [found[ref] for ref in sorted(found)]


@dataclass(frozen=True)
class LiqwidSnapshot:
    """Every live Liqwid V2 record, classified, with the context the views read.

    ``context`` holds every live MarketParams, CollateralParams, oracle feed and
    MarketState UTxO, in out-ref order, with no slot: it is the parse context of the
    live tip, built from the same fetch as the market states and loans.
    """

    market_states: tuple[LiqwidRecord, ...]
    loans: tuple[LiqwidRecord, ...]
    context: ParseContext
    now_ms: int


def snapshot(
    backend: AbstractBackend,
    now_ms: int | None = None,
    deployment: LiqwidDeployment | None = None,
) -> LiqwidSnapshot:
    """Fetch and classify every live Liqwid V2 UTxO the deployment's selectors name.

    ``now_ms`` (POSIX ms) is the evaluation time; it defaults to now. ``deployment`` is
    the bundled mainnet one by default.
    """
    now_ms = resolve_now_ms(now_ms)
    records = fetch_records(
        backend,
        liqwid_selectors(deployment),
        deployment=deployment,
    )
    return LiqwidSnapshot(
        market_states=tuple(
            record for record in records if record.kind is LiqwidKind.MARKET_STATE
        ),
        loans=tuple(record for record in records if record.kind is LiqwidKind.LOAN),
        context=ParseContext(
            slot=None,
            utxos=tuple(
                record.info for record in records if record.kind is not LiqwidKind.LOAN
            ),
        ),
        now_ms=now_ms,
    )
