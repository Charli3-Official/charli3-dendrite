"""Forward (redeemer-less) collateral pricing from live source leaves.

`leaf_rate` turns one concrete leaf into its forward rate via the verified parsers in
`reproduce.py`; the path/price walk and the loader seam build on it (later tasks).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from charli3_dendrite.lending.danogo.oracles.leaves import liqwid_oracle_feed_id
from charli3_dendrite.lending.danogo.oracles.leaves import parse_liqwid_oracle_v2
from charli3_dendrite.lending.danogo.oracles.leaves import parse_minswap_lp
from charli3_dendrite.lending.danogo.oracles.locator import LeafHandle
from charli3_dendrite.lending.danogo.oracles.locator import Recipe
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.oracles.reproduce import leaf_forward_rates
from charli3_dendrite.lending.danogo.oracles.reproduce import splash_lp_candidate
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo


def leaf_rate(
    otype: str,
    datum: str | None,
    assets: tuple[tuple[str, str, int], ...],
    *,
    quote_unit: str,
) -> Fraction | None:
    """Forward rate a single leaf contributes, or None if it does not parse.

    `otype` is an `OracleUtxoType` member *name*. Splash LP leaves price in `quote_unit`
    (only if the quote is a pool asset); all other kinds are quote-agnostic.
    """
    try:
        ot = OracleUtxoType[otype]
    except KeyError:
        return None
    leaf = OracleLeaf(otype=ot, datum=datum, assets=assets)
    rates = leaf_forward_rates(leaf)
    if rates:
        return rates[0][1]
    return splash_lp_candidate(leaf, quote_unit)


def leaf_discriminator(otype: str, datum: str | None) -> str:
    """Stable per-UTxO identity for source kinds whose on-chain *value* is not unique.

    Some source kinds live many-to-one behind a shared marker token at a common
    address, so the value alone cannot pin a UTxO; a stable datum field can:

    * Liqwid V2 oracles -> ``feed_id:priced_asset``. Neither part is unique on its own
      (one feed id spans many assets; one asset, e.g. ADA, can have several feeds), but
      together they pin the UTxO.
    * Minswap pools -> the ``asset_a|asset_b`` pair (all pools carry one MSP token).

    Returns "" for kinds already pinned by a unique NFT (no datum disambiguation
    needed) or on any parse failure.
    """
    if datum is None:
        return ""
    try:
        if otype == OracleUtxoType.TLIQWID_ORACLE_V2.name:
            return f"{liqwid_oracle_feed_id(datum)}:{parse_liqwid_oracle_v2(datum)[0]}"
        if otype == OracleUtxoType.TMINSWAP_LP.name:
            asset_a, _, asset_b, _ = parse_minswap_lp(datum)
            return f"{asset_a}|{asset_b}"
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return ""


@dataclass(frozen=True)
class LiveLeaf:
    """A source leaf as currently read on-chain: its parser type, datum, and value."""

    otype: str
    datum: str | None
    assets: tuple[tuple[str, str, int], ...] = ()


def replay_recipe(
    steps: list[tuple[LiveLeaf, bool, int]],
    *,
    quote_unit: str,
) -> tuple[int, int] | None:
    """Compose a collateral price from an ordered, fixed set of source leaves.

    Each step is `(leaf, is_reverse, scale_exp)`: the leaf's forward rate (inverted when
    `is_reverse`) times `10**scale_exp`; the steps multiply into one rational. Returns
    `(num, denom)`, or None if any leaf fails to parse or yields a non-positive rate.
    """
    price = Fraction(1)
    for leaf, is_reverse, scale_exp in steps:
        rate = leaf_rate(leaf.otype, leaf.datum, leaf.assets, quote_unit=quote_unit)
        if rate is None or rate <= 0:
            return None
        hop = (1 / rate) if is_reverse else rate
        price *= hop * (Fraction(10) ** scale_exp)
    if price <= 0:
        return None
    return (price.numerator, price.denominator)


# A collateral whose own pool prices it in ADA (its recipe leads with a Danogo pool
# leaf) but whose market quote is a different token is priced THROUGH ADA: the value
# is ``collateral -> ada`` (the leading pool leg) times ``ada -> quote`` (the canonical
# intermediate recipe). ADA is the universal intermediate quote.
INTERMEDIATE_QUOTE = "lovelace"

# External price-feed leaf kinds (Liqwid-v2 / Djed / Indigo) that a collateral recipe
# can lead with as its OWN embedded ``ada -> quote`` bridge. That embedded bridge need
# not be the quote's canonical standalone intermediate feed: the on-chain validator
# prices the standalone ``ada -> quote`` as its own path config entry (a possibly
# different feed), so a price-feed-leading collateral is priced whole and the canonical
# intermediate is declared independently. Named (not the enum) to avoid importing the
# `_common` source-kind groupings here.
_ADA_BRIDGE_FEED_OTYPES = frozenset(
    {
        OracleUtxoType.TLIQWID_ORACLE_V2.name,
        OracleUtxoType.TDJED.name,
        OracleUtxoType.TINDIGO.name,
    },
)


def _scaled_product(
    ada_leg: tuple[int, int] | None,
    intermediate: tuple[int, int],
) -> tuple[int, int] | None:
    """``ada_leg * intermediate`` as a reduced ``(num, denom)``, or None if no leg."""
    if ada_leg is None:
        return None
    composed = Fraction(*ada_leg) * Fraction(*intermediate)
    return (composed.numerator, composed.denominator)


def compose_cross_quote(
    quote: str,
    collat_steps: list[tuple[LiveLeaf, bool, int]],
    intermediate_steps: list[tuple[LiveLeaf, bool, int]] | None,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Compose a cross-quote collateral price through ADA, or None if not cross-quote.

    Fires only when the market quote is not ADA itself and a resolved canonical
    ``ada -> quote`` intermediate recipe is supplied. The standalone intermediate price
    declared is ALWAYS that canonical recipe's rate (the validator prices the
    ``ada -> quote`` path from its own path config entry, a feed that need not be the
    bridge a collateral recipe embeds). Three recipe conventions are handled:

    * *price-feed-leading* -- the recipe leads with an external price-feed leaf
      (Liqwid-v2 / Djed / Indigo) that is its OWN ``ada -> quote`` bridge and differs
      from the canonical intermediate feed. The collateral is priced whole over its
      own leaves (``replay_recipe``); the canonical intermediate is declared separately.
    * *intermediate-leading* -- the recipe's leading steps ARE the canonical
      ``ada -> quote`` intermediate (the same leaves, byte-for-byte); the trailing
      steps price the collateral in ADA (``collat_steps[n:]``), multiplied by the
      intermediate.
    * *pool-leading* -- the recipe leads with a Danogo pool leaf and carries a trailing
      ADA->quote bridge step. The collateral is valued in ADA by its leading pool leg
      (``collat_steps[:-1]``); the trailing step only approximates ADA->quote and is
      dropped in favour of the resolved intermediate.

    Returns the composed ``collateral -> quote`` rate and the intermediate
    ``ada -> quote`` rate; None leaves single-hop pricing unchanged.

    This is the one shared cross-quote rate composition: both the read-only analytics
    resolver and the transaction builder feed it their own resolved leaves.
    """
    if quote == INTERMEDIATE_QUOTE or not intermediate_steps:
        return None
    intermediate_to_quote = replay_recipe(intermediate_steps, quote_unit=quote)
    if intermediate_to_quote is None:
        return None
    n = len(intermediate_steps)
    lead = collat_steps[0][0].otype if collat_steps else None
    enough = len(collat_steps) >= 2  # noqa: PLR2004
    if (
        enough
        and lead in _ADA_BRIDGE_FEED_OTYPES
        and collat_steps[:n] != intermediate_steps
    ):
        # Price-feed-leading: the collateral recipe embeds its own ``ada -> quote``
        # bridge feed, which differs from the quote's canonical intermediate feed. Price
        # the collateral whole over its own leaves; the canonical intermediate is the
        # standalone feed, declared independently (the validator walks its own path).
        collat_price = replay_recipe(collat_steps, quote_unit=quote)
    elif len(collat_steps) > n and collat_steps[:n] == intermediate_steps:
        ada_leg = replay_recipe(collat_steps[n:], quote_unit=INTERMEDIATE_QUOTE)
        collat_price = _scaled_product(ada_leg, intermediate_to_quote)
    elif enough and lead == OracleUtxoType.TDANOGO_POOL.name:
        ada_leg = replay_recipe(collat_steps[:-1], quote_unit=INTERMEDIATE_QUOTE)
        collat_price = _scaled_product(ada_leg, intermediate_to_quote)
    else:
        return None
    if collat_price is None:
        return None
    return collat_price, intermediate_to_quote


def resolve_leaf_info(
    backend: AbstractBackend,
    handle: LeafHandle,
) -> PoolStateInfo | None:
    """Resolve the single live UTxO a handle pins, as the backend's `PoolStateInfo`.

    A unique identifying NFT yields a single match; kinds that share a marker token
    across many feeds at one address (e.g. Liqwid V2 oracles) are narrowed by
    `handle.datum_key` -- the stable asset-identity parsed from each candidate's datum.
    Returns None unless exactly one UTxO matches, so the caller never reads the wrong
    feed. This is the shared resolution both `read_live_leaf` (forward pricing) and the
    create-loan snapshot (full-UTxO assembly) rely on.
    """
    want = (handle.nft_policy + handle.nft_name) if handle.nft_policy else None
    rows = list(
        backend.get_pool_utxos(
            addresses=[handle.address],
            assets=[want] if want else None,
            historical=False,
        ),
    )
    matches = [info for info in rows if want is None or want in info.assets.root]
    if handle.datum_key:
        matches = [
            info
            for info in matches
            if leaf_discriminator(handle.otype, info.datum_cbor) == handle.datum_key
        ]
    if len(matches) != 1:
        return None
    return matches[0]


def read_live_leaf(backend: AbstractBackend, handle: LeafHandle) -> LiveLeaf | None:
    """Fetch the current UTxO for a handle (address + identifying NFT) as a LiveLeaf.

    Resolves to a leaf only when the handle pins exactly one UTxO (see
    `resolve_leaf_info`). Otherwise returns None so the collateral stays unpriced rather
    than risk reading the wrong feed.
    """
    info = resolve_leaf_info(backend, handle)
    if info is None:
        return None
    assets = tuple(
        ("", "", int(q)) if unit == "lovelace" else (unit[:56], unit[56:], int(q))
        for unit, q in info.assets.root.items()
    )
    return LiveLeaf(otype=handle.otype, datum=info.datum_cbor, assets=assets)


def _read_recipe_steps(
    backend: AbstractBackend,
    recipe: Recipe,
) -> list[tuple[LiveLeaf, bool, int]] | None:
    """Read each recipe step's current source leaf into a replay step list.

    Returns the ordered ``(leaf, is_reverse, scale_exp)`` steps, or None if any step's
    leaf cannot be resolved live (so the collateral stays unpriced rather than risk
    reading the wrong feed) or the recipe is empty.
    """
    steps: list[tuple[LiveLeaf, bool, int]] = []
    for step in recipe.steps:
        live = read_live_leaf(backend, step.handle)
        if live is None:
            return None
        steps.append((live, step.is_reverse, step.scale_exp))
    return steps or None


def resolve_prices(
    backend: AbstractBackend,
    *,
    registry: dict[str, Recipe],
    pairs: Iterable[tuple[str, str]],
) -> PriceMap:
    """Price each (collateral_unit, quote_unit) pair via its recipe over live leaves.

    For each pair, look up ``f"{collateral}|{quote}"`` in the registry, read every
    step's current source leaf, and replay the recipe in the market's actual quote.

    Collateral whose pool prices it in ADA while the market quote is a different token
    is composed through ADA (``collateral -> ada`` times the canonical ``ada -> quote``
    intermediate recipe), exactly as the transaction builder does via the shared
    `compose_cross_quote`. Such a pair emits two entries -- the intermediate
    ``ada -> quote`` price and the composed ``collateral -> quote`` price -- so the
    analytics basis matches the on-chain redeemer.

    Emits ``OraclePrice(token=..., quote=quote, ...)``. A pair with no recipe, a
    missing leaf, or a non-reproducing walk is simply omitted (collateral stays unpriced
    -> not-liquidatable). Never raises into the caller.

    NOTE: ``PriceMap`` keys by token only, so a collateral used across markets with
    different supply tokens can hold only one quote. That's a pre-existing PriceMap
    constraint, not handled here.
    """
    pm = PriceMap()
    for collateral, quote in pairs:
        try:
            recipe = registry.get(f"{collateral}|{quote}")
            if recipe is None:
                continue
            steps = _read_recipe_steps(backend, recipe)
            if steps is None:
                continue

            intermediate_recipe = registry.get(f"{INTERMEDIATE_QUOTE}|{quote}")
            intermediate_steps = (
                _read_recipe_steps(backend, intermediate_recipe)
                if intermediate_recipe is not None
                else None
            )
            composed = compose_cross_quote(quote, steps, intermediate_steps)
            if composed is not None:
                collat_price, intermediate_price = composed
                pm.add(
                    OraclePrice(
                        token=INTERMEDIATE_QUOTE,
                        quote=quote,
                        num=intermediate_price[0],
                        denom=intermediate_price[1],
                        source=OracleSource.DANOGO_AGGREGATOR,
                    ),
                )
                pm.add(
                    OraclePrice(
                        token=collateral,
                        quote=quote,
                        num=collat_price[0],
                        denom=collat_price[1],
                        source=OracleSource.DANOGO_AGGREGATOR,
                    ),
                )
                continue

            got = replay_recipe(steps, quote_unit=quote)
            if got is None:
                continue
            pm.add(
                OraclePrice(
                    token=collateral,
                    quote=quote,
                    num=got[0],
                    denom=got[1],
                    source=OracleSource.DANOGO_AGGREGATOR,
                ),
            )
        except Exception:  # noqa: BLE001, S112 - a pricing outage must never crash
            continue
    return pm
