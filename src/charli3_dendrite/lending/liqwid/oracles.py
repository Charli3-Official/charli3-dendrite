"""Liqwid V2 on-chain oracle feeds: parsing, per-asset selection, lovelace prices.

A feed is a UTxO that holds the oracle token at one of the oracle validators, with
the datum ``[updater, asset, [num, den], ts_ms, validity_ms(, caps)]``. The price is
USD per 1,000,000 base units of the asset. Liqwid binds a feed by oracle token and
asset, whatever its address, so the asset is the feed's identity; the updater key
rotates and is carried as data only. Token-less outputs at an oracle validator are
not feeds, however well-formed their datum.

Importing this module registers ``LiqwidOracleResolver`` for
``OracleSource.LIQWID``; importing ``charli3_dendrite.lending`` alone does not.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import Final

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import OracleFeedDatum
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.models import register_resolver
from charli3_dendrite.lending.units import script_payment_address

ADA_UNIT: Final[str] = "lovelace"
POLICY_KEY_PREFIX: Final[str] = "policy:"


@dataclass(frozen=True)
class LiqwidFeed:
    """One Liqwid oracle feed: USD per 1,000,000 base units of ``asset_unit``.

    ``asset_unit`` is a dendrite unit (``"lovelace"`` or policy + name hex) for an
    exact feed, and the bare 56-hex policy for a policy-wide one. ``field4`` is the
    datum's validity window in milliseconds; ``caps`` is the hard-caps field of the
    six-field layout (None in the five-field layout).
    """

    out_ref: str
    asset_unit: str
    policy_wide: bool
    usd_num: int
    usd_den: int
    ts_ms: int
    updater: str
    field4: int
    caps: object | None = field(hash=False, compare=False)

    @property
    def asset_key(self) -> str:
        """Identity of the priced asset; a policy-wide feed never reads as a unit."""
        if self.policy_wide:
            return POLICY_KEY_PREFIX + self.asset_unit
        return self.asset_unit


def _out_ref_key(out_ref: str) -> tuple[str, int]:
    """``(tx_hash, index)`` of an out-ref, the index compared as an integer."""
    tx_hash, _, index = out_ref.partition("#")
    return (tx_hash, int(index) if index.isdigit() else -1)


def _rank(feed: LiqwidFeed) -> tuple[int, str, int]:
    return (feed.ts_ms, *_out_ref_key(feed.out_ref))


def _priced(feed: LiqwidFeed) -> bool:
    return feed.usd_num > 0 and feed.usd_den > 0


def _lovelace_rational(feed: LiqwidFeed, ada: LiqwidFeed) -> tuple[int, int] | None:
    """Lovelace per base unit of ``feed``'s asset as ``(nX * dA, dX * nA)``.

    Both prices are per 1,000,000 base units and ADA's base unit is the lovelace, so
    the scale cancels exactly. None for a policy-wide feed or a non-positive price.
    """
    if feed.policy_wide or not _priced(feed) or not _priced(ada):
        return None
    return (feed.usd_num * ada.usd_den, feed.usd_den * ada.usd_num)


def parse_feed(
    info: PoolStateInfo,
    deployment: LiqwidDeployment | None = None,
) -> LiqwidFeed | None:
    """Decode one UTxO into a feed; None, never raising, unless it is genuine.

    Genuineness is :func:`~charli3_dendrite.lending.liqwid.indexing.parse_utxo`'s:
    the UTxO sits at one of the deployment's oracle validators, holds exactly one
    oracle token (quantity 1, empty name) and nothing else under that policy, and
    its inline datum decodes as a five- or six-field feed record.
    """
    record = parse_utxo(info, deployment)
    return _feed_of(record) if record is not None else None


def _feed_of(record: LiqwidRecord) -> LiqwidFeed | None:
    """The feed a parsed record carries; None for any other kind of record."""
    datum = record.datum
    if record.kind is not LiqwidKind.ORACLE or not isinstance(datum, OracleFeedDatum):
        return None
    return LiqwidFeed(
        out_ref=record.out_ref,
        asset_unit=datum.asset,
        policy_wide=datum.policy_wide,
        usd_num=datum.price[0],
        usd_den=datum.price[1],
        ts_ms=datum.ts_ms,
        updater=datum.updater,
        field4=datum.validity_ms,
        caps=datum.caps,
    )


def select_feeds(feeds: Iterable[LiqwidFeed]) -> dict[str, LiqwidFeed]:
    """The freshest feed per asset, keyed by ``asset_key``.

    Freshest is the greatest ``ts_ms``; equal timestamps go to the greater out-ref
    (transaction hash, then output index as an integer), so the result never depends
    on the order of ``feeds``.
    """
    chosen: dict[str, LiqwidFeed] = {}
    for feed in feeds:
        current = chosen.get(feed.asset_key)
        if current is None or _rank(feed) > _rank(current):
            chosen[feed.asset_key] = feed
    return chosen


def liqwid_price_map(feeds: Iterable[LiqwidFeed]) -> PriceMap:
    """Lovelace price per base unit of every exact, non-zero asset in ``feeds``.

    Feeds are selected first (``select_feeds``). Each price is
    ``(nX * dA) / (dX * nA)`` for the asset's ``nX / dX`` and ADA's ``nA / dA``, with
    ``valid_from`` = the feed's timestamp. A zero price, a policy-wide feed, or a
    missing or zero ADA feed yields no entry.
    """
    selected = select_feeds(feeds)
    prices = PriceMap()
    ada = selected.get(ADA_UNIT)
    if ada is None:
        return prices
    for feed in selected.values():
        rational = _lovelace_rational(feed, ada)
        if rational is None:
            continue
        prices.add(
            OraclePrice(
                token=feed.asset_unit,
                quote=ADA_UNIT,
                num=rational[0],
                denom=rational[1],
                source=OracleSource.LIQWID,
                valid_from=feed.ts_ms,
            ),
        )
    return prices


def _market_state(
    market: MarketComponents,
    records: Iterable[LiqwidRecord],
) -> MarketStateDatum | None:
    """The freshest MarketState of ``market`` among parsed ``records``.

    Genuineness is ``parse_utxo``'s (the market's NFT at one of its MarketState
    validators, a decodable datum). Freshest is the greatest last-batch time, then
    the greatest last-interest time, then the greater out-ref.
    """
    best: MarketStateDatum | None = None
    best_rank: tuple[int, int, str, int] | None = None
    for record in records:
        state = record.datum
        if record.kind is not LiqwidKind.MARKET_STATE:
            continue
        if record.market_id != market.market_id:
            continue
        if not isinstance(state, MarketStateDatum):
            continue
        rank = (
            state.last_batch_ms,
            state.last_interest_ms,
            *_out_ref_key(record.out_ref),
        )
        if best_rank is None or rank > best_rank:
            best, best_rank = state, rank
    return best


def _append_unique(
    selectors: list[PoolSelector],
    seen: set[tuple[tuple[str, ...], str]],
    addresses: list[str],
    policy: str,
) -> None:
    key = (tuple(addresses), policy)
    if key in seen:
        return
    seen.add(key)
    selectors.append(PoolSelector(addresses=addresses, assets=[policy]))


class LiqwidOracleResolver:
    """Prices a Liqwid qToken in lovelace from on-chain feeds and its MarketState.

    The ref's ``token`` is the qToken unit; ``extra["underlying"]`` names the
    underlying unit and ``extra["market_state"]`` the market's MarketState NFT
    policy. The price is s9 (underlying per qToken) times the underlying's lovelace
    price from the Liqwid feeds.
    """

    source = OracleSource.LIQWID

    def __init__(self, deployment: LiqwidDeployment | None = None) -> None:
        """Bind a deployment; None resolves the bundled one on first use."""
        self._deployment = deployment

    @property
    def deployment(self) -> LiqwidDeployment:
        """The bound deployment, or the bundled default."""
        if self._deployment is not None:
            return self._deployment
        return default_deployment()

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """Every oracle validator filtered by the oracle token, then the MarketStates.

        One selector per oracle validator, then one per distinct MarketState NFT
        policy named by ``refs`` (over all of that market's MarketState validators).
        A ref naming no known market adds no MarketState selector.
        """
        active = self.deployment
        selectors: list[PoolSelector] = []
        seen: set[tuple[tuple[str, ...], str]] = set()
        for validator in active.oracle_validators:
            _append_unique(
                selectors,
                seen,
                [script_payment_address(validator.script_hash)],
                active.oracle_token_policy,
            )
        for ref in refs:
            policy = ref.extra.get("market_state")
            if not isinstance(policy, str):
                continue
            market = active.market_of_state_policy(policy)
            if market is None:
                continue
            _append_unique(
                selectors,
                seen,
                [script_payment_address(h) for h in market.market_state_validators],
                market.market_state_policy,
            )
        return selectors

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """``s9 * nX * dA / (s9_den * dX * nA)`` lovelace per qToken base unit.

        None when ``ref`` names no known market, not that market's qToken or not its
        underlying, or when the underlying feed, the ADA feed, a non-zero price or
        the MarketState is missing among ``utxos``.
        """
        active = self.deployment
        underlying = ref.extra.get("underlying")
        policy = ref.extra.get("market_state")
        if not isinstance(underlying, str) or not isinstance(policy, str):
            return None
        market = active.market_of_state_policy(policy)
        if market is None or ref.token != market.qtoken_policy:
            return None
        if underlying != market.underlying:
            return None
        parsed = (parse_utxo(info, active) for info in utxos)
        records = [record for record in parsed if record is not None]
        feeds = select_feeds(
            feed for feed in (_feed_of(record) for record in records) if feed
        )
        ada, priced = feeds.get(ADA_UNIT), feeds.get(underlying)
        rational: tuple[int, int] | None = None
        if ada is not None and priced is not None:
            rational = _lovelace_rational(priced, ada)
        state = _market_state(market, records)
        if rational is None or state is None:
            return None
        s9_num, s9_den = state.qtoken_rate
        if s9_num <= 0 or s9_den <= 0:
            return None
        return OraclePrice(
            token=ref.token,
            quote=ADA_UNIT,
            num=s9_num * rational[0],
            denom=s9_den * rational[1],
            source=self.source,
        )


register_resolver(LiqwidOracleResolver())
