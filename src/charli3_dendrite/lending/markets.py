"""Market sources: every lending protocol's markets, positions and requests together.

Each protocol registers one :class:`MarketSource`. A source names the UTxOs to watch
(each one either a view or context a view reads), converts one UTxO into its common
view (pure, and safe on spent UTxOs, so an indexer can use it over history), and
loads a live snapshot from a backend. A protocol whose loans carry no identity token
also exposes a :class:`PositionLifecycle` that chains each loan to its successor.

:class:`LendingMarketBook` compares the markets: where a borrow is cheapest, where a
supply earns most, and which requests a lender could fill. It sits beside the
loan-monitoring ``LendingBook``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from importlib import import_module
from typing import TYPE_CHECKING
from typing import Final
from typing import Protocol
from typing import TypeVar
from typing import runtime_checkable

from charli3_dendrite.lending.builtins import BUILTIN_PROTOCOL_PACKAGES
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.rates import MS_PER_HOUR

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable
    from collections.abc import Sequence

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.liqwid.lifecycle import LoanKey
    from charli3_dendrite.lending.liqwid.lifecycle import LoanSpend
    from charli3_dendrite.lending.liqwid.lifecycle import LoanTransition
    from charli3_dendrite.lending.normalized import BorrowRequest
    from charli3_dendrite.lending.normalized import LendingMarket
    from charli3_dendrite.lending.normalized import LendingPosition
    from charli3_dendrite.lending.oracles.models import PriceMap

VIEW_ROLE: Final = "view"
CONTEXT_ROLE: Final = "context"


@dataclass(frozen=True)
class WatchSpec:
    """UTxOs worth reading: a payment credential, and the identity policy if any.

    ``role`` says what a matching UTxO is for: :data:`VIEW_ROLE` (the default) is one
    :meth:`MarketSource.parse` may turn into a view; :data:`CONTEXT_ROLE` only feeds a
    :class:`ParseContext`, and ``parse`` returns None for it.
    """

    payment_credential: str
    identity_policy: str | None
    role: str = VIEW_ROLE


@dataclass(frozen=True)
class ParseContext:
    """UTxOs a view depends on, as of one chain point.

    ``slot`` is the point's slot (None when not known). ``utxos`` holds every
    context-role UTxO unspent at that point and may also hold view-role UTxOs a view
    reads; a parser selects what it needs by identity token.
    """

    slot: int | None
    utxos: tuple[PoolStateInfo, ...] = ()


@runtime_checkable
class PositionLifecycle(Protocol):
    """Chained positions: a loan has no identity token, so successors are matched.

    A protocol whose loan UTxO is re-created under a new out-ref on every change
    exposes one of these as :attr:`MarketSource.lifecycle`.
    """

    # Policies of loan tokens many loans share: never a position's identity.
    loan_token_policies: frozenset[str]

    def loan_key(self, info: PoolStateInfo) -> LoanKey | None:
        """What successor matching compares for ``info``; None when it is no loan."""
        ...

    def match_successors(
        self,
        spent: Sequence[LoanKey],
        created: Sequence[LoanKey],
    ) -> dict[str, str]:
        """Spent out-ref -> created out-ref for the loans one transaction re-created."""
        ...

    def classify_spend(
        self,
        spent: LoanKey,
        successor: LoanKey | None,
        spend: LoanSpend,
    ) -> LoanTransition:
        """What the transaction that spent ``spent`` did to it."""
        ...


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

    # Read-only, so a source may hold it as a plain attribute or a narrower type.
    @property
    def lifecycle(self) -> PositionLifecycle | None:
        """How chained positions are followed; None when a token is the identity."""
        ...

    def selectors(self) -> list[WatchSpec]:
        """The UTxOs :meth:`parse` converts, and the context UTxOs it reads."""
        ...

    def parse(
        self,
        info: PoolStateInfo,
        *,
        context: ParseContext | None = None,
    ) -> LendingMarket | LendingPosition | BorrowRequest | None:
        """One UTxO as its common view; None when it is not one of this protocol's.

        ``context`` holds the other UTxOs the view depends on, as of the UTxO's chain
        point; a source whose views stand alone ignores it. A context-role UTxO is
        never a view: ``parse`` returns None for it.

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


_T = TypeVar("_T")
_V = TypeVar("_V")


def convertible_views(
    convert: Callable[[_T], _V],
    states: Iterable[_T],
) -> tuple[_V, ...]:
    """``convert`` over ``states`` in order, leaving out those that raise.

    A state whose conversion raises LendingParseError is skipped; any other error
    propagates. :meth:`MarketSource.load` implementations build their views with it.
    """
    out: list[_V] = []
    for state in states:
        try:
            out.append(convert(state))
        except LendingParseError:
            continue
    return tuple(out)


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
    """Register every built-in protocol's market source, once, on first use."""
    global _BUILTINS_LOADED  # noqa: PLW0603
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    for module_name in BUILTIN_PROTOCOL_PACKAGES:
        source: MarketSource = import_module(module_name).market_source()
        # A caller's own registration under the same name is kept.
        _SOURCES.setdefault(source.name.lower(), source)


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

        A market qualifies when it lends ``borrow_unit``, accepts the collateral, can
        lend ``amount`` now (its liquidity, within any borrow cap), and (for a term
        loan) its term covers ``duration_ms``.

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
            and market.borrowable_now() >= amount
            and _lasts(market, duration_ms)
            and market.terms_for(collateral_unit) is not None
            and (include_permissioned or not market.permissioned)
        ]
        return sorted(options, key=lambda o: (o.interest, o.market.market_id))

    def lend_options(
        self,
        borrow_unit: str,
        now_ms: int | None = None,
        *,
        include_permissioned: bool = False,
    ) -> list[BorrowRequest]:
        """Requests to borrow ``borrow_unit`` a lender could fill, best rate first.

        With ``now_ms``, requests already expired then are left out (a fill must fall
        before the expiry); permissioned requests are left out unless asked for.
        """
        return sorted(
            (
                r
                for r in self.requests
                if r.borrow_unit == borrow_unit
                and (now_ms is None or now_ms < r.expires_ms)
                and (include_permissioned or not r.permissioned)
            ),
            key=lambda r: (-r.rate_model.headline_rate(), r.request_id),
        )

    def supply_options(
        self,
        unit: str,
        *,
        include_permissioned: bool = False,
    ) -> list[LendingMarket]:
        """Markets that take a supply of ``unit`` for a receipt token, best rate first.

        A market qualifies when it lends ``unit`` and issues a receipt token. A market
        without a supply rate ranks after every market with one; ties go to the lower
        market id. Permissioned markets are left out unless asked for.
        """
        return sorted(
            (
                m
                for m in self.markets
                if m.borrow_unit == unit
                and m.receipt_unit is not None
                and (include_permissioned or not m.permissioned)
            ),
            key=_supply_rank,
        )


def _lasts(market: LendingMarket, duration_ms: int) -> bool:
    """Whether ``market``'s term, if it has one, lasts ``duration_ms``."""
    term = market.rate_model.term_hours()
    return term is None or term * MS_PER_HOUR >= duration_ms


def _supply_rank(market: LendingMarket) -> tuple[bool, Fraction, str]:
    """Sort key: markets with a supply rate first, highest rate first, then by id."""
    if market.supply_rate is None:
        return (True, Fraction(0), market.market_id)
    return (False, -market.supply_rate, market.market_id)
