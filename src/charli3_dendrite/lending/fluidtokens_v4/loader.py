"""Backend-driven discovery of FluidTokens V4 UTxOs.

A convenience over :func:`~charli3_dendrite.lending.fluidtokens_v4.indexing.parse_utxo`:
resolve the live config, fetch every unspent UTxO at each V4 spend-script credential,
parse them, and link each loan to its live pool. Prices are an input; nothing here
fetches them.

Selectors are enterprise addresses matched by payment credential, which assumes a
backend that selects by payment credential (as the dbsync backend does); on a backend
that matches exact addresses the snapshot comes back empty.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import TypeVar

from charli3_dendrite.lending.base import LendingBook
from charli3_dendrite.lending.fluidtokens_v4.constants import resolve_config
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntitySelector
from charli3_dendrite.lending.fluidtokens_v4.indexing import FluidV4State
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.indexing import parse_utxo
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4AssetManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LenderManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LoanState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LockedBorrowerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4RequestState

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
    from charli3_dendrite.lending.oracles.models import PriceMap

PAGE_SIZE = 1000

_T = TypeVar("_T")


def fetch_entities(
    backend: AbstractBackend,
    selector: EntitySelector,
    *,
    page_size: int = PAGE_SIZE,
) -> list[FluidV4State]:
    """Every unspent UTxO at ``selector``'s credential that parses as its kind.

    Pages through ``get_pool_utxos`` and terminates on a short page or on a page that
    adds no new out-ref, so a backend that ignores ``page`` and returns the same rows
    again ends the loop. Pages are de-duplicated by out-ref, since the backend pages
    with LIMIT / OFFSET. Sorted by out-ref.
    """
    only = {selector.kind: selector}
    found: dict[str, FluidV4State] = {}
    seen: set[tuple[str, int]] = set()
    page = 0
    while True:
        rows = list(
            backend.get_pool_utxos(
                addresses=[selector.address],
                limit=page_size,
                page=page,
                historical=False,
            ),
        )
        before = len(seen)
        for info in rows:
            seen.add((info.tx_hash, info.tx_index))
            state = parse_utxo(info, only)
            if state is not None:
                found.setdefault(state.out_ref, state)
        if len(rows) < page_size or len(seen) == before:
            return [found[ref] for ref in sorted(found)]
        page += 1


def link_loans(
    pools: list[FluidV4PoolState],
    loans: list[FluidV4LoanState],
    now_ms: int,
) -> None:
    """Set the evaluation time on every loan and attach each live origin pool.

    A loan whose pool is gone (cancelled) keeps no pool context but still evaluates:
    its terms are in its own datum.
    """
    by_id = {pool.pool_id: pool for pool in pools}
    for loan in loans:
        pool = by_id.get(loan.pool_id)
        if pool is None:
            loan.set_time(now_ms)
        else:
            loan.attach_context(pool=pool, market=pool.market, now_ms=now_ms)


@dataclass
class FluidV4Snapshot:
    """Every live V4 UTxO, parsed, at one evaluation time."""

    config: ConfigDatum
    now_ms: int
    pools: list[FluidV4PoolState]
    pool_managers: list[FluidV4PoolManagerState]
    loans: list[FluidV4LoanState]
    requests: list[FluidV4RequestState]
    asset_managers: list[FluidV4AssetManagerState]
    lender_managers: list[FluidV4LenderManagerState]
    locked_borrower_managers: list[FluidV4LockedBorrowerState]

    def book(self, prices: PriceMap | None = None) -> LendingBook:
        """A lending book over every loan, priced with ``prices``."""
        return LendingBook.from_loans(
            list(self.loans),
            prices=prices,
            block_time=self.now_ms,
        )


def _of(states: list[FluidV4State], cls: type[_T]) -> list[_T]:
    return [state for state in states if isinstance(state, cls)]


def snapshot(
    backend: AbstractBackend,
    *,
    now_ms: int | None = None,
) -> FluidV4Snapshot:
    """Fetch and parse every live V4 UTxO, following the live config.

    ``now_ms`` (POSIX ms) is the loans' evaluation time; it defaults to now.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    config = resolve_config(backend)
    fetched = {
        kind: fetch_entities(backend, selector)
        for kind, selector in entity_selectors(config).items()
    }
    pools = _of(fetched[EntityKind.POOL], FluidV4PoolState)
    loans = _of(fetched[EntityKind.LOAN], FluidV4LoanState)
    link_loans(pools, loans, now_ms)
    return FluidV4Snapshot(
        config=config,
        now_ms=now_ms,
        pools=pools,
        pool_managers=_of(fetched[EntityKind.POOL_MANAGER], FluidV4PoolManagerState),
        loans=loans,
        requests=_of(fetched[EntityKind.REQUEST], FluidV4RequestState),
        asset_managers=_of(fetched[EntityKind.ASSET_MANAGER], FluidV4AssetManagerState),
        lender_managers=_of(
            fetched[EntityKind.LENDER_MANAGER],
            FluidV4LenderManagerState,
        ),
        locked_borrower_managers=_of(
            fetched[EntityKind.LOCKED_BORROWER_MANAGER],
            FluidV4LockedBorrowerState,
        ),
    )
