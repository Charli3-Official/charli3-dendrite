"""Danogo snapshot loader: discover, fetch, parse, stitch, build a LendingBook.

`build_book` is the pure, deterministic core (no I/O). `snapshot` is the
backend-driven path: resolve the deployment scripts, fetch the market-param /
pool-state / loan UTxOs, parse them, stitch loan->pool/market context, and build
a `LendingBook`.

Collateral is priced live (redeemer-less) by default: `snapshot` replays each
market's `collateral|supply_token` recipe (from the packaged registry) over
freshly-read source leaves via the observation-based forward resolver. A caller
may still supply an explicit `prices` map to bypass forward resolution; loans
whose collateral stays unpriced are reported as not-liquidatable (safe under
outage).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pycardano import Address

from charli3_dendrite.lending.base import LendingBook
from charli3_dendrite.lending.danogo.constants import DanogoScriptAddresses
from charli3_dendrite.lending.danogo.constants import resolve_addresses
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.state import DanogoLoanState
from charli3_dendrite.lending.danogo.state import DanogoPoolState

if TYPE_CHECKING:
    from collections.abc import Collection

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.oracles.models import PriceMap


def build_book(
    *,
    pools: list[DanogoPoolState],
    loans: list[DanogoLoanState],
    prices: PriceMap,
    now_ms: int,
) -> LendingBook:
    """Pure stitching: attach prices/time and produce a LendingBook.

    Pools/loans must already have their market (and loan->pool) context attached by
    the caller. This function is the deterministic, side-effect-free core.

    `LendingBook` stores a single `pool`, but Danogo is multi-market, so `book.pool`
    only reflects `pools[0]`. This does NOT affect correctness: per-loan debt and
    health-factor use each loan's own attached `_pool` context (which is what all
    `LendingBook` methods actually rely on), so results are correct regardless of which
    pool lands in `book.pool`. A multi-pool-aware `LendingBook` is a candidate refactor
    when a second multi-market protocol is added.
    """
    pool = pools[0] if pools else None
    return LendingBook.from_loans(loans, prices=prices, pool=pool, block_time=now_ms)


def _payment_cred_hex(address: str) -> str:
    """Payment-credential (script hash) hex of an enterprise/base address."""
    return Address.decode(address).payment_part.payload.hex()


def _pool_state(info: PoolStateInfo) -> DanogoPoolState:
    return DanogoPoolState(
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


def _loan_state(info: PoolStateInfo) -> DanogoLoanState:
    return DanogoLoanState(
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


def _pool_nft_name(info: PoolStateInfo, policy: str) -> str | None:
    """Asset name (hex) of the pool NFT (policy == config_pool script hash).

    The pool NFT identifies a market and tags both the market-param UTxO and the
    pool-state UTxO, so it links the two. Returns None if no such asset is present.
    """
    for unit, qty in info.assets.root.items():
        if unit != "lovelace" and unit.startswith(policy) and qty == 1:
            name = unit[len(policy) :]
            if name:
                return name
    return None


def _loan_market_name(
    assets: Assets,
    loan_policy: str,
    valid_names: Collection[str],
) -> str | None:
    """Pool-NFT name (hex) of the market a loan belongs to, from its loan-identity NFT.

    Each loan UTxO carries a loan token minted under the loan script hash whose asset
    name IS the pool-NFT name of the loan's market (`create_loan` mints it that way).
    Mirroring `_pool_nft_name`, this reads that name directly off the loan rather than
    guessing the market from the borrowed supply token (several markets can share a
    supply token) or the collateral (several markets can accept the same collateral).
    Returns the name only when it is a known market/pool name (in `valid_names`), else
    None (the loan is skipped, matching the existing skip-on-parse-failure degrade).
    """
    for unit, qty in assets.root.items():
        if unit != "lovelace" and unit.startswith(loan_policy) and qty == 1:
            name = unit[len(loan_policy) :]
            if name in valid_names:
                return name
    return None


def fetch_markets(
    backend: AbstractBackend,
    addresses: DanogoScriptAddresses,
) -> dict[str, DanogoMarket]:
    """Market-param UTxOs (at the config-pool script) keyed by pool-NFT name."""
    policy = _payment_cred_hex(addresses["config_pool"])
    markets: dict[str, DanogoMarket] = {}
    for info in backend.get_pool_utxos(
        addresses=[addresses["config_pool"]],
        historical=False,
    ):
        name = _pool_nft_name(info, policy)
        if name is None:
            continue
        try:
            markets[name] = DanogoMarket.from_market_datum(info.datum_cbor)
        except (ValueError, IndexError, TypeError, AttributeError):
            # Not a market-param UTxO (e.g. an unrelated config UTxO); skip.
            continue
    return markets


def fetch_pools(
    backend: AbstractBackend,
    addresses: DanogoScriptAddresses,
) -> dict[str, DanogoPoolState]:
    """Pool-state UTxOs (at the pool script) keyed by pool-NFT name."""
    policy = _payment_cred_hex(addresses["config_pool"])
    pools: dict[str, DanogoPoolState] = {}
    for info in backend.get_pool_utxos(
        addresses=[addresses["pool"]],
        historical=False,
    ):
        name = _pool_nft_name(info, policy)
        if name is None:
            continue
        pools[name] = _pool_state(info)
    return pools


def fetch_loans(
    backend: AbstractBackend,
    addresses: DanogoScriptAddresses,
) -> list[DanogoLoanState]:
    """All loan-position UTxOs (at the loan script)."""
    return [
        _loan_state(info)
        for info in backend.get_pool_utxos(
            addresses=[addresses["loan"]],
            historical=False,
        )
    ]


def snapshot(
    backend: AbstractBackend,
    *,
    now_ms: int | None = None,
    prices: PriceMap | None = None,
) -> LendingBook:
    """Discover + fetch + parse + stitch a live Danogo snapshot into a LendingBook.

    Steps: resolve the four deployment scripts, fetch market-param / pool-state /
    loan UTxOs, pair each pool with its market by pool-NFT name, attach each loan to
    the pool/market whose supply token it borrows, then build the book.

    `now_ms` is the interest-accrual evaluation time (defaults to wall-clock now).
    `prices` is the collateral `PriceMap`. When omitted, collateral is priced live via
    the observation-based forward resolver (redeemer-less: replay each market's
    `collateral|supply_token` recipe over freshly-read source leaves); unpriced
    collateral degrades to not-liquidatable, never a crash. Pass `prices` explicitly to
    bypass forward resolution.
    """
    from charli3_dendrite.lending.oracles.models import PriceMap

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    addresses = resolve_addresses(backend)
    markets = fetch_markets(backend, addresses)
    pools_by_name = fetch_pools(backend, addresses)
    loans = fetch_loans(backend, addresses)

    # Default: price collateral live via the observation-based forward resolver, one
    # (collateral, supply_token) pair per market. A caller-supplied `prices` bypasses
    # this. A pricing outage degrades to an empty map (collateral not-liquidatable),
    # never a crash.
    if prices is None:
        try:
            from charli3_dendrite.lending.danogo.oracles import forward
            from charli3_dendrite.lending.danogo.oracles.locator import (
                augment_registry_with_bond_dtokens,
            )
            from charli3_dendrite.lending.danogo.oracles.locator import load_registry

            registry = load_registry()
            # Bond dTokens (a pool's own redeemable dToken) are accepted as collateral
            # but are not mined for every (dToken, quote); synthesize their pricing
            # recipes from the live pool/market state before resolving, so a dToken
            # collateral prices instead of collapsing the loan's health factor.
            augment_registry_with_bond_dtokens(
                registry,
                pool_address=addresses["pool"],
                config_pool_cred=_payment_cred_hex(addresses["config_pool"]),
                markets=markets,
            )
            pairs = {
                (collateral, market.supply_token)
                for market in markets.values()
                for collateral in market.collaterals
            }
            prices = forward.resolve_prices(backend, registry=registry, pairs=pairs)
        except Exception:  # noqa: BLE001 - pricing outage must not crash the snapshot
            prices = PriceMap()

    # Pair pool <-> market (same pool-NFT name) and index BY THAT NAME. Each loan is
    # stitched to the market its own loan-identity NFT names -- not one guessed from its
    # borrowed supply token or collateral, both of which several markets can share.
    pools: list[DanogoPoolState] = []
    by_name: dict[str, tuple[DanogoPoolState, DanogoMarket]] = {}
    for name, pool in pools_by_name.items():
        market = markets.get(name)
        if market is None:
            continue
        pool.attach_market(market)
        pools.append(pool)
        by_name[name] = (pool, market)

    loan_policy = _payment_cred_hex(addresses["loan"])
    stitched: list[DanogoLoanState] = []
    for loan in loans:
        loan_market = _loan_market_name(loan.assets, loan_policy, by_name)
        if loan_market is None:
            continue
        pool, market = by_name[loan_market]
        loan.attach_context(pool=pool, market=market, now_ms=now_ms)
        stitched.append(loan)

    return build_book(pools=pools, loans=stitched, prices=prices, now_ms=now_ms)
