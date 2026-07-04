"""FluidTokens snapshot loader: discover, fetch, parse, stitch, build a book.

`build_book` is the pure, deterministic core (no I/O). `snapshot` is the
backend-driven path: resolve the entity addresses, fetch the pool / loan / request
UTxOs, parse them, stitch each loan to its origin pool/market, and build a
`LendingBook`.

FluidTokens is single-config: pools, loans, and requests live at three fixed
entity addresses, each UTxO tagged with an identity NFT under its policy. A pool is
keyed by its pool-NFT name; a loan references its origin pool via the datum
`origin_id`. Collateral is priced best-effort via `feeds.resolve_prices`; a pricing
outage degrades to an empty map (collateral not-liquidatable), never a crash.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from charli3_dendrite.lending.base import LendingBook
from charli3_dendrite.lending.fluidtokens.constants import resolve_addresses
from charli3_dendrite.lending.fluidtokens.market import FluidMarket
from charli3_dendrite.lending.fluidtokens.oracles import feeds
from charli3_dendrite.lending.fluidtokens.state import FluidLoanState
from charli3_dendrite.lending.fluidtokens.state import FluidPoolState
from charli3_dendrite.lending.fluidtokens.state import FluidRequestState

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.fluidtokens.constants import FluidScriptAddresses
    from charli3_dendrite.lending.oracles.models import PriceMap

# `origin_id` is the pool-NFT name prefixed by this 4-byte tag (b"POOL").
_POOL_ORIGIN_TAG = b"POOL"


def build_book(
    *,
    pools: list[FluidPoolState],
    loans: list[FluidLoanState],
    prices: PriceMap,
    now_ms: int,
) -> LendingBook:
    """Pure stitching: attach prices/time and produce a LendingBook.

    Pools/loans must already have their market (and loan->pool) context attached by
    the caller. This function is the deterministic, side-effect-free core.

    `LendingBook` stores a single `pool`, but FluidTokens is multi-pool, so
    `book.pool` only reflects `pools[0]`. This does NOT affect correctness: per-loan
    debt and health-factor use each loan's own attached `_pool` context (which is what
    all `LendingBook` methods actually rely on), so results are correct regardless of
    which pool lands in `book.pool`. A multi-pool-aware `LendingBook` is a candidate
    refactor when the aggregation views need to span pools.
    """
    pool = pools[0] if pools else None
    return LendingBook.from_loans(loans, prices=prices, pool=pool, block_time=now_ms)


def _pool_state(info: PoolStateInfo) -> FluidPoolState:
    return FluidPoolState(
        address=info.address,
        assets=info.assets,
        block_time=info.block_time,
        block_index=info.block_index,
        plutus_v2=info.plutus_v2,
        datum_cbor=info.datum_cbor,
        datum_hash=info.datum_hash,
        tx_index=info.tx_index,
        tx_hash=info.tx_hash,
    )


def _loan_state(info: PoolStateInfo) -> FluidLoanState:
    return FluidLoanState(
        address=info.address,
        assets=info.assets,
        block_time=info.block_time,
        block_index=info.block_index,
        plutus_v2=info.plutus_v2,
        datum_cbor=info.datum_cbor,
        datum_hash=info.datum_hash,
        tx_index=info.tx_index,
        tx_hash=info.tx_hash,
    )


def _request_state(info: PoolStateInfo) -> FluidRequestState:
    return FluidRequestState(
        address=info.address,
        assets=info.assets,
        datum_cbor=info.datum_cbor,
    )


def _nft_name(info: PoolStateInfo, policy: str) -> str | None:
    """Asset name (hex) of the identity NFT (policy-prefixed, qty 1, non-empty).

    Returns None when the UTxO carries no such identity NFT.
    """
    for unit, qty in info.assets.root.items():
        if unit != "lovelace" and unit.startswith(policy) and qty == 1:
            name = unit[len(policy) :]
            if name:
                return name
    return None


def fetch_pools(
    backend: AbstractBackend,
    addresses: FluidScriptAddresses,
) -> dict[str, FluidPoolState]:
    """Pool UTxOs (at the pool address) keyed by pool-NFT name."""
    policy = addresses["pool_policy"]
    pools: dict[str, FluidPoolState] = {}
    for info in backend.get_pool_utxos(
        addresses=[addresses["pool"]],
        historical=False,
    ):
        name = _nft_name(info, policy)
        if name is None:
            continue
        pools[name] = _pool_state(info)
    return pools


def fetch_loans(
    backend: AbstractBackend,
    addresses: FluidScriptAddresses,
) -> list[FluidLoanState]:
    """All loan UTxOs (at the loan address)."""
    return [
        _loan_state(info)
        for info in backend.get_pool_utxos(
            addresses=[addresses["loan"]],
            historical=False,
        )
    ]


def fetch_requests(
    backend: AbstractBackend,
    addresses: FluidScriptAddresses,
) -> list[FluidRequestState]:
    """All request UTxOs (at the request address)."""
    return [
        _request_state(info)
        for info in backend.get_pool_utxos(
            addresses=[addresses["request"]],
            historical=False,
        )
    ]


def _pool_name_for_origin(origin_id: bytes) -> str:
    """Pool-NFT name (hex) referenced by a loan's `origin_id`.

    Strip a leading `b"POOL"` tag if present, then return the remaining hex; fall
    back to the full origin hex so an untagged future layout still matches.
    """
    if origin_id.startswith(_POOL_ORIGIN_TAG):
        return origin_id[len(_POOL_ORIGIN_TAG) :].hex()
    return origin_id.hex()


def snapshot(
    backend: AbstractBackend,
    *,
    now_ms: int | None = None,
    prices: PriceMap | None = None,
) -> LendingBook:
    """Discover + fetch + parse + stitch a live FluidTokens snapshot into a book.

    Steps: resolve the three entity addresses, fetch pool / loan / request UTxOs,
    derive each pool's `FluidMarket` from its datum, index pools by NFT name, attach
    each loan to the pool/market its `origin_id` references, then build the book.

    `now_ms` is the interest-accrual evaluation time (defaults to wall-clock now).
    `prices` is the collateral `PriceMap`. When omitted, collateral is priced
    best-effort via `feeds.resolve_prices`; a pricing outage degrades to an empty map
    (collateral not-liquidatable), never a crash. Requests are fetched and parsed for
    validation of the request address/policy wiring; `LendingBook` tracks only loans,
    so they are not attached to the book.
    """
    from charli3_dendrite.lending.oracles.models import PriceMap

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    addresses = resolve_addresses(backend)
    pools_by_name = fetch_pools(backend, addresses)
    loans = fetch_loans(backend, addresses)
    # Parsing requests proves the request address/policy wiring; they are not part of
    # the loan-only LendingBook, so the parsed views are intentionally not retained.
    fetch_requests(backend, addresses)

    # Pair each pool with the market derived from its datum, indexed by NFT name so
    # loans can find their origin pool/market.
    pools: list[FluidPoolState] = []
    by_name: dict[str, tuple[FluidPoolState, FluidMarket]] = {}
    for name, pool in pools_by_name.items():
        try:
            market = FluidMarket.from_pool_datum(pool.pool_datum)
        except (ValueError, IndexError, TypeError, AttributeError):
            # Not a parseable pool datum (e.g. an unrelated UTxO); skip.
            continue
        pool.attach_market(market)
        pools.append(pool)
        by_name[name] = (pool, market)

    # Default: price collateral best-effort. A caller-supplied `prices` bypasses this.
    if prices is None:
        try:
            pairs = {
                (collateral, market.principal_unit)
                for _, market in by_name.values()
                for collateral in market.collateral_units
            }
            prices = feeds.resolve_prices(backend, pairs=pairs)
        except Exception:  # noqa: BLE001 - pricing outage must not crash the snapshot
            prices = PriceMap()

    stitched: list[FluidLoanState] = []
    for loan in loans:
        try:
            origin_id = loan.loan_datum.origin_id  # type: ignore[attr-defined]
        except (ValueError, IndexError, TypeError, AttributeError):
            continue
        ctx = by_name.get(_pool_name_for_origin(origin_id))
        if ctx is None:
            continue
        pool, market = ctx
        loan.attach_context(pool=pool, market=market, now_ms=now_ms)
        stitched.append(loan)

    return build_book(pools=pools, loans=stitched, prices=prices, now_ms=now_ms)
