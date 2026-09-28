"""Market sources: every lending protocol's markets, positions and requests together.

Each protocol registers one :class:`MarketSource`. A source names the UTxOs to watch,
converts one UTxO into its common view (pure, and safe on spent UTxOs, so an indexer
can use it over history), and loads a live snapshot from a backend.

:class:`LendingMarketBook` compares the markets: where a borrow is cheapest, and which
requests a lender could fill. It sits beside the loan-monitoring ``LendingBook``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.normalized import BorrowRequest
    from charli3_dendrite.lending.normalized import LendingMarket
    from charli3_dendrite.lending.normalized import LendingPosition
    from charli3_dendrite.lending.oracles.models import PriceMap


@dataclass(frozen=True)
class WatchSpec:
    """UTxOs worth reading: a payment credential, and the identity policy if any."""

    payment_credential: str
    identity_policy: str | None


@dataclass(frozen=True)
class LendingSnapshot:
    """Every live market, position and request of one or more protocols."""

    markets: tuple[LendingMarket, ...] = ()
    positions: tuple[LendingPosition, ...] = ()
    requests: tuple[BorrowRequest, ...] = ()

    def merged(self, other: LendingSnapshot) -> LendingSnapshot:
        """This snapshot followed by ``other``."""
        return LendingSnapshot(
            markets=self.markets + other.markets,
            positions=self.positions + other.positions,
            requests=self.requests + other.requests,
        )


class MarketSource(Protocol):
    """One lending protocol's markets, positions and requests."""

    name: str

    def selectors(self) -> list[WatchSpec]:
        """The UTxOs :meth:`parse` converts."""
        ...

    def parse(
        self,
        info: PoolStateInfo,
    ) -> LendingMarket | LendingPosition | BorrowRequest | None:
        """One UTxO as its common view; None when it is not one of this protocol's.

        Raises LendingParseError for a UTxO it recognizes but cannot convert.
        """
        ...

    def load(
        self,
        backend: AbstractBackend,
        now_ms: int | None = None,
    ) -> LendingSnapshot:
        """Everything live, from ``backend``."""
        ...


_SOURCES: dict[str, MarketSource] = {}
_BUILTINS_LOADED = False


def register_market_source(source: MarketSource) -> None:
    """Register (or replace) a source under its name, case-insensitively."""
    _SOURCES[source.name.lower()] = source


def get_market_source(name: str) -> MarketSource:
    """The source registered under ``name``.

    Raises:
        KeyError: if none is, naming the known sources.
    """
    _load_builtins()
    try:
        return _SOURCES[name.lower()]
    except KeyError:
        known = ", ".join(available_market_sources()) or "(none registered)"
        raise KeyError(
            f"no market source registered for {name!r}; known sources: {known}",
        ) from None


def available_market_sources() -> list[str]:
    """Registered source names, sorted."""
    _load_builtins()
    return sorted(_SOURCES)


def load_all(backend: AbstractBackend, now_ms: int | None = None) -> LendingSnapshot:
    """Every registered source's live snapshot, merged in name order."""
    snapshot = LendingSnapshot()
    for name in available_market_sources():
        snapshot = snapshot.merged(_SOURCES[name].load(backend, now_ms))
    return snapshot


def _load_builtins() -> None:
    """Register the sources shipped with the library, once, on first use."""
    global _BUILTINS_LOADED  # noqa: PLW0603
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from charli3_dendrite.lending.fluidtokens_v4.market import FluidTokensV4MarketSource

    register_market_source(FluidTokensV4MarketSource())


@dataclass(frozen=True)
class BorrowOption:
    """A market that can serve a borrow, what it costs, and the collateral it needs."""

    market: LendingMarket
    interest: int
    required_collateral: int | None


class LendingMarketBook:
    """Markets and requests to compare across protocols."""

    def __init__(
        self,
        markets: Iterable[LendingMarket],
        requests: Iterable[BorrowRequest] = (),
    ) -> None:
        """Hold ``markets`` and ``requests``."""
        self.markets = tuple(markets)
        self.requests = tuple(requests)

    @classmethod
    def from_snapshot(cls, snapshot: LendingSnapshot) -> LendingMarketBook:
        """A book over a snapshot's markets and requests."""
        return cls(snapshot.markets, snapshot.requests)

    def borrow_options(
        self,
        borrow_unit: str,
        collateral_unit: str,
        amount: int,
        duration_ms: int,
        prices: PriceMap | None = None,
        *,
        include_permissioned: bool = False,
    ) -> list[BorrowOption]:
        """Markets that can lend ``amount`` against ``collateral_unit``, cheapest first.

        Cost is the interest ``amount`` owes over ``duration_ms`` on each market's own
        terms; ties go to the lower market id. ``required_collateral`` is None where a
        needed price is missing.
        """
        options = [
            BorrowOption(
                market=market,
                interest=market.rate_model.interest_for(amount, duration_ms),
                required_collateral=market.required_collateral(
                    collateral_unit,
                    amount,
                    prices,
                ),
            )
            for market in self.markets
            if market.borrow_unit == borrow_unit
            and market.available_liquidity >= amount
            and market.terms_for(collateral_unit) is not None
            and (include_permissioned or not market.permissioned)
        ]
        return sorted(options, key=lambda o: (o.interest, o.market.market_id))

    def lend_options(self, borrow_unit: str) -> list[BorrowRequest]:
        """Open requests to borrow ``borrow_unit``, highest headline rate first."""
        return sorted(
            (r for r in self.requests if r.borrow_unit == borrow_unit),
            key=lambda r: (-r.rate_model.headline_rate(), r.request_id),
        )
