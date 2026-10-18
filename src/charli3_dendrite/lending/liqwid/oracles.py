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
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
from charli3_dendrite.lending.liqwid.indexing import chain_position
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import newest_record
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.models import register_resolver
from charli3_dendrite.lending.oracles.qtoken import receipt_price
from charli3_dendrite.lending.units import out_ref_key

ADA_UNIT: Final[str] = "lovelace"
POLICY_KEY_PREFIX: Final[str] = "policy:"


@dataclass(frozen=True)
class LiqwidFeed:
    """One Liqwid oracle feed: USD per 1,000,000 base units of ``asset_unit``.

    ``asset_unit`` is a dendrite unit (``"lovelace"`` or policy + name hex) for an
    exact feed, and the bare 56-hex policy for a policy-wide one. ``field4`` is the
    datum's validity window in milliseconds; ``caps`` is the hard-caps field of the
    six-field layout (None in the five-field layout). ``info`` is the UTxO the feed
    was read from (None for a feed built by hand); it takes no part in equality or
    hashing.
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
    info: PoolStateInfo | None = field(
        default=None,
        hash=False,
        compare=False,
        repr=False,
    )

    @property
    def asset_key(self) -> str:
        """Identity of the priced asset; a policy-wide feed never reads as a unit."""
        if self.policy_wide:
            return POLICY_KEY_PREFIX + self.asset_unit
        return self.asset_unit


def _rank(feed: LiqwidFeed) -> tuple[int, int, int, str, int]:
    """Feed time, then chain position (from ``info``, the out-ref without it)."""
    # A feed built without its UTxO has no block: it ranks below every read feed of
    # the same millisecond, and among such feeds by out-ref.
    if feed.info is not None:
        return (feed.ts_ms, *chain_position(feed.info))
    return (feed.ts_ms, -1, -1, *out_ref_key(feed.out_ref))


def lovelace_rational(
    usd: tuple[int, int],
    ada: tuple[int, int],
) -> tuple[int, int] | None:
    """Lovelace per base unit of an asset as ``(nX * dA, dX * nA)``, unreduced.

    ``usd`` is the asset's feed price ``(nX, dX)`` and ``ada`` ADA's ``(nA, dA)``,
    both USD per 1,000,000 base units. ADA's base unit is the lovelace, so the scale
    cancels exactly. None when either price is not positive: a missing or zero ADA
    price prices nothing.
    """
    usd_num, usd_den = usd
    ada_num, ada_den = ada
    if usd_num <= 0 or usd_den <= 0 or ada_num <= 0 or ada_den <= 0:
        return None
    return (usd_num * ada_den, usd_den * ada_num)


def _lovelace_rational(feed: LiqwidFeed, ada: LiqwidFeed) -> tuple[int, int] | None:
    """:func:`lovelace_rational` of two feeds; None for a policy-wide ``feed``."""
    if feed.policy_wide:
        return None
    return lovelace_rational(
        (feed.usd_num, feed.usd_den),
        (ada.usd_num, ada.usd_den),
    )


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
        info=record.info,
    )


def select_feeds(feeds: Iterable[LiqwidFeed]) -> dict[str, LiqwidFeed]:
    """The freshest feed per asset, keyed by ``asset_key``.

    Freshest is the greatest ``ts_ms``; equal timestamps go to the later chain
    position of the feed's UTxO (``info``; see
    :func:`~charli3_dendrite.lending.liqwid.indexing.chain_position`), so the result
    never depends on the order of ``feeds``. A feed built without its UTxO ranks
    below every read feed of its millisecond, and among such feeds by out-ref
    (transaction hash, then output index as an integer).
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
    """The newest MarketState of ``market`` among parsed ``records``.

    Genuineness is ``parse_utxo``'s (the market's NFT at one of its MarketState
    validators, a decodable datum). Newest is the latest chain position
    (:func:`~charli3_dendrite.lending.liqwid.indexing.newest_record`), the same
    state the market view binds.
    """
    record = newest_record(records, LiqwidKind.MARKET_STATE, market.market_id)
    state = None if record is None else record.datum
    return state if isinstance(state, MarketStateDatum) else None


class LiqwidOracleResolver:
    """Prices a Liqwid qToken in lovelace from on-chain feeds and its MarketState.

    The ref's ``token`` is the qToken unit; ``extra["underlying"]`` names the
    underlying unit and ``extra["market_state"]`` the market's MarketState NFT
    policy. The price is s9 (underlying per qToken) times the underlying's lovelace
    price in :func:`liqwid_price_map` of the feeds, by
    :func:`~charli3_dendrite.lending.oracles.qtoken.receipt_price`.

    The UTxOs passed to :meth:`resolve` must be the unspent outputs at
    :meth:`selectors` (a backend read with ``historical=False``). The resolver cannot
    tell a spent output from an unspent one: given historical outputs it prices from
    the freshest feed and the newest MarketState among them, which is a past price
    whenever the read leaves out the current ones (a paged historical read returns
    the oldest first).
    """

    source = OracleSource.LIQWID

    def __init__(self, deployment: LiqwidDeployment | None = None) -> None:
        """Bind a deployment; None resolves the bundled one on first use."""
        self._deployment = deployment

    @property
    def deployment(self) -> LiqwidDeployment:
        """The bound deployment, or the bundled default."""
        return resolve_deployment(self._deployment)

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """Every oracle validator filtered by the oracle token, then the MarketStates.

        Both are grouped from the oracle and MarketState selectors of
        :func:`~charli3_dendrite.lending.liqwid.indexing.liqwid_selectors`: one
        selector per oracle validator, then one per distinct MarketState NFT policy
        named by ``refs``, over all of that market's MarketState validators.
        A ref naming no known market adds no MarketState selector. Read each
        selector's unspent outputs only (``historical=False``) and pass them to
        :meth:`resolve`; a historical read yields stale prices (see the class
        docstring).
        """
        oracles: dict[tuple[str, str], None] = {}
        states: dict[str, list[str]] = {}
        for selector in liqwid_selectors(self.deployment):
            if selector.kind is LiqwidKind.ORACLE:
                oracles.setdefault((selector.address, selector.identity_policy), None)
            elif selector.kind is LiqwidKind.MARKET_STATE:
                policy = selector.identity_policy
                states.setdefault(policy, []).append(selector.address)
        named = dict.fromkeys(
            policy
            for policy in (ref.extra.get("market_state") for ref in refs)
            if isinstance(policy, str) and policy in states
        )
        return [
            *(
                PoolSelector(addresses=[address], assets=[policy])
                for address, policy in oracles
            ),
            *(
                PoolSelector(addresses=list(states[policy]), assets=[policy])
                for policy in named
            ),
        ]

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """s9 times the underlying's lovelace price, per qToken base unit.

        For a native-token underlying the price is
        ``s9_num * nX * dA / (s9_den * dX * nA)``; ADA is worth one lovelace, so a
        qADA price is s9 itself, with or without an ADA feed. ``valid_from`` is the
        underlying feed's timestamp (the ADA feed's for qADA; None without one).

        None when ``ref`` names no known market, not that market's qToken or not its
        underlying, or when the MarketState, a positive s9 or a positive underlying
        price (its feed and the ADA feed) is missing among ``utxos``.

        ``utxos`` must be the unspent outputs at :meth:`selectors`
        (``historical=False``). Spent outputs are not detected: the freshest feed
        and the newest MarketState among ``utxos`` win, so a historical read that
        misses the current outputs returns a past price with no signal.
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
        state = _market_state(market, records)
        if state is None:
            return None
        prices = liqwid_price_map(
            feed for feed in (_feed_of(record) for record in records) if feed
        )
        entry = prices.get(underlying)
        return receipt_price(
            ref.token,
            state.qtoken_rate,
            underlying,
            prices,
            source=self.source,
            valid_from=None if entry is None else entry.valid_from,
        )


register_resolver(LiqwidOracleResolver())
