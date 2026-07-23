"""Normalized oracle price model + resolver registry.

Every supported oracle source is parsed into a single `OraclePrice` so the
lending base classes never branch on source. Resolvers are registered at
import time and dispatched by `OracleSource`.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Protocol
from typing import runtime_checkable

from pydantic import Field
from pydantic import field_serializer
from pydantic import field_validator

from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList


class OracleSource(str, Enum):
    """Identifies which resolver parses a feed."""

    CHARLI3 = "charli3"
    ORCFAX = "orcfax"
    FLUID_AGGREGATED = "fluid_aggregated"
    FLUID_POOLED = "fluid_pooled"
    FLUID_DEDICATED = "fluid_dedicated"
    QTOKEN_RATE = "qtoken_rate"
    DEX_POOLED = "dex_pooled"
    DANOGO_AGGREGATOR = "danogo_aggregator"


# Oracle sources owned by the shared lending layer. Resolvers for these are
# registered when `charli3_dendrite.lending` is imported. Protocol-specific
# sources (e.g. DANOGO_AGGREGATOR) are registered by their own subpackage on
# import, keeping the shared layer protocol-agnostic.
CORE_ORACLE_SOURCES: frozenset[OracleSource] = frozenset(
    {
        OracleSource.CHARLI3,
        OracleSource.ORCFAX,
        OracleSource.FLUID_AGGREGATED,
        OracleSource.FLUID_POOLED,
        OracleSource.FLUID_DEDICATED,
        OracleSource.QTOKEN_RATE,
        OracleSource.DEX_POOLED,
    },
)


class OraclePrice(DendriteBaseModel):
    """Price of `token` denominated in `quote`, as the rational num/denom."""

    token: str
    quote: str = "lovelace"
    num: int
    denom: int
    source: OracleSource
    valid_from: int | None = None
    valid_to: int | None = None

    def as_decimal(self) -> Decimal:
        """Return the price as a Decimal (num / denom)."""
        if self.denom == 0:
            raise ZeroDivisionError("price denominator is zero")
        return Decimal(self.num) / Decimal(self.denom)

    def is_fresh(self, at_ms: int) -> bool:
        """Return True if `at_ms` falls within the validity window."""
        if self.valid_from is not None and at_ms < self.valid_from:
            return False
        return not (self.valid_to is not None and at_ms > self.valid_to)


class OracleRef(DendriteBaseModel):
    """Describes where to find a price feed and what it prices.

    Resolvers read whatever subset of fields they need. `extra` carries
    source-specific data (e.g. a precomputed qToken rate, DEX reserves).
    """

    source: OracleSource
    token: str
    quote: str = "lovelace"
    feed_policy: str | None = None
    feed_name: str | None = None
    feed_id: str | None = None
    address: str | None = None
    decimals: int = 0
    extra: dict = Field(default_factory=dict)

    def selector(self) -> PoolSelector | None:
        """Default selector: feed address + optional feed-NFT asset."""
        if self.address is None and self.feed_policy is None:
            return None
        assets = None
        if self.feed_policy is not None:
            assets = [self.feed_policy + (self.feed_name or "")]
        return PoolSelector(
            addresses=[self.address] if self.address else [],
            assets=assets,
        )


class PriceMap(DendriteBaseModel):
    """(token, quote) -> freshest OraclePrice.

    A collateral can be priced under more than one market quote (e.g. ADA priced
    into several supply tokens), so the identity is the price's own ``(token,
    quote)`` pair, not the token alone. Keying by token alone let same-token,
    different-quote prices collide, making reads non-deterministic; keying by the
    pair keeps each quote's price distinct.
    """

    prices: dict[tuple[str, str], OraclePrice] = Field(default_factory=dict)

    # The `(token, quote)` tuple keys have no symmetric JSON form: pydantic dumps a
    # tuple key as ``"token,quote"`` but cannot parse it back. Encode each key as
    # ``"token|quote"`` on dump and split it back on validate so both `model_dump`/
    # `model_validate` and `model_dump_json`/`model_validate_json` round-trip. Units are
    # hex or "lovelace" and never contain ``|``.
    @field_serializer("prices")
    def _serialize_prices(
        self,
        prices: dict[tuple[str, str], OraclePrice],
    ) -> dict[str, OraclePrice]:
        return {f"{token}|{quote}": price for (token, quote), price in prices.items()}

    @field_validator("prices", mode="before")
    @classmethod
    def _parse_price_keys(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        parsed: dict[object, object] = {}
        for key, price in value.items():
            if isinstance(key, str):
                token, quote = key.split("|", 1)
                parsed[(token, quote)] = price
            else:
                parsed[key] = price
        return parsed

    def add(self, price: OraclePrice) -> None:
        """Insert `price` under its ``(token, quote)``, keeping the latest `valid_to`.

        A `valid_to` of `None` means "no expiry" and is treated as the freshest
        possible value (`+inf`), so an open-ended price is never overwritten by
        a dated one and always overwrites a dated one for the same ``(token, quote)``.
        """

        def _key(p: OraclePrice) -> float:
            return float("inf") if p.valid_to is None else p.valid_to

        pair = (price.token, price.quote)
        existing = self.prices.get(pair)
        if existing is None or _key(price) >= _key(existing):
            self.prices[pair] = price

    def get(self, token: str, quote: str = "lovelace") -> OraclePrice | None:
        """Return the stored price for ``(token, quote)``, or None if absent."""
        return self.prices.get((token, quote))

    def require(self, token: str, quote: str = "lovelace") -> OraclePrice:
        """Return the stored price for ``(token, quote)``, or raise KeyError."""
        price = self.prices.get((token, quote))
        if price is None:
            raise KeyError(f"no price for {token} in {quote}")
        return price


@runtime_checkable
class PriceResolver(Protocol):
    """Parses a feed UTxO of one `OracleSource` into an `OraclePrice`."""

    source: OracleSource

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """UTxO selectors needed to resolve these refs."""
        ...

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Find + parse the feed for `ref` among `utxos`."""
        ...


_REGISTRY: dict[OracleSource, PriceResolver] = {}


def register_resolver(resolver: PriceResolver) -> None:
    """Register (or replace) the resolver for a source."""
    _REGISTRY[resolver.source] = resolver


def get_resolver(source: OracleSource) -> PriceResolver:
    """Return the resolver for a source, or raise KeyError."""
    return _REGISTRY[source]


def resolver_sources() -> set[OracleSource]:
    """Sources with a registered resolver."""
    return set(_REGISTRY)
