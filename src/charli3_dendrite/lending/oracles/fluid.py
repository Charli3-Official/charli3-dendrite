"""FluidTokens OraclePriceFeed variants + resolver.

Feeds are in-band Plutus data (passed via OracleRef.extra['feed_cbor']):
    Aggregated = Constr 0 [common, price_in_lovelaces, price_denominator]
    Pooled     = Constr 1 [common, token_a_amount, token_b_amount, fees_per_mille]
    Dedicated  = Constr 2 [common, price_in_lovelaces, price_denominator]
    common     = Constr 0 [valid_from, valid_to, Asset]
Charli3/Orcfax variants are handled by their dedicated resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass

from pycardano import DeserializeException
from pycardano import PlutusData

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource


@dataclass
class FluidAsset(PlutusData):
    """Asset identifier (policy id + asset name) of a Fluid feed token."""

    CONSTR_ID = 0
    policy_id: bytes
    asset_name: bytes


@dataclass
class FluidCommonFeedData(PlutusData):
    """Shared feed header: validity window and priced token."""

    CONSTR_ID = 0
    valid_from: int
    valid_to: int
    token: FluidAsset


@dataclass
class FluidAggregated(PlutusData):
    """Aggregated feed: price quoted directly as lovelaces / denominator."""

    CONSTR_ID = 0
    common: FluidCommonFeedData
    token_price_in_lovelaces: int
    token_price_denominator: int


@dataclass
class FluidPooled(PlutusData):
    """Pooled feed: price derived from on-chain reserves of two pool tokens."""

    CONSTR_ID = 1
    common: FluidCommonFeedData
    token_a_amount_in_pool: int
    token_b_amount_in_pool: int
    pool_fees_per_mille: int


@dataclass
class FluidDedicated(PlutusData):
    """Dedicated feed: price quoted directly as lovelaces / denominator."""

    CONSTR_ID = 2
    common: FluidCommonFeedData
    price_in_lovelaces: int
    price_denominator: int


_VARIANTS: tuple[type[PlutusData], ...] = (
    FluidAggregated,
    FluidPooled,
    FluidDedicated,
)


def _parse_feed(cbor_hex: str) -> PlutusData | None:
    """Decode `cbor_hex` into the first matching Fluid feed variant, or None."""
    for cls in _VARIANTS:
        # A shape/constructor mismatch surfaces as DeserializeException; a
        # malformed (e.g. non-hex) feed_cbor surfaces as TypeError/ValueError/
        # IndexError from the underlying decode. Treat all as "not this variant".
        try:
            return cls.from_cbor(cbor_hex)
        except (DeserializeException, TypeError, ValueError, IndexError):
            continue
    return None


class FluidResolver:
    """Resolves a Fluid OraclePriceFeed (Aggregated/Pooled/Dedicated)."""

    def __init__(self, source: OracleSource) -> None:
        """Bind the resolver to the `OracleSource` it labels emitted prices with."""
        self.source = source

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """Feed data is in-band, not fetched as a UTxO, so no selectors."""
        return []

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Parse the in-band feed CBOR for `ref` into an OraclePrice."""
        cbor = ref.extra.get("feed_cbor")
        if not cbor:
            return None
        feed = _parse_feed(cbor)
        if feed is None:
            return None
        if isinstance(feed, FluidPooled):
            # Assumed orientation: token_a is the priced/base token, token_b is
            # the quote, so price(token_a in token_b) = token_b / token_a (hence
            # num=token_b_amount, denom=token_a_amount). Orientation not yet
            # confirmed against a real Fluid feed.
            num, denom = feed.token_b_amount_in_pool, feed.token_a_amount_in_pool
        elif isinstance(feed, FluidAggregated):
            num, denom = feed.token_price_in_lovelaces, feed.token_price_denominator
        else:  # FluidDedicated
            num, denom = feed.price_in_lovelaces, feed.price_denominator
        # Refuse a non-positive rational that would later raise ZeroDivisionError
        # or invert sign in OraclePrice.as_decimal().
        if denom <= 0 or num <= 0:
            return None
        return OraclePrice(
            token=ref.token,
            quote=ref.quote,
            num=num,
            denom=denom,
            source=self.source,
            valid_from=feed.common.valid_from,
            valid_to=feed.common.valid_to,
        )
