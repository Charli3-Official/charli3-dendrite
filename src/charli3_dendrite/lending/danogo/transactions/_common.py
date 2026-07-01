"""Shared building blocks for the Danogo forward transaction builders.

The per-action builders (`create_loan`/`build.py`, `topup_withdraw.py`, `repay.py`)
all drive the same oracle Withdraw and share the UTxO conversion, reference-input
ordering, validity-window, oracle config selection, alt-supply revaluation, and actor
funding helpers. Those live here -- the leaf module the builders import from -- so the
logic is defined once and never re-inlined per action.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeAlias

import cbor2  # type: ignore[import-not-found]
from pycardano import Address
from pycardano import Network
from pycardano import PlutusData
from pycardano import PlutusV3Script
from pycardano import RawCBOR
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import VerificationKeyHash
from pycardano import Withdrawals

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.danogo.constants import MINSWAP_LP_POLICY
from charli3_dendrite.lending.danogo.constants import ORACLE_PATH_FT_POLICY
from charli3_dendrite.lending.danogo.constants import ORACLE_QUOTE_ANCHOR
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import PRational
from charli3_dendrite.lending.danogo.math import total_collateral_val_with_threshold
from charli3_dendrite.lending.danogo.oracles.aggregator_datums import OraclePathDatum
from charli3_dendrite.lending.danogo.oracles.deployments import OracleDeployment
from charli3_dendrite.lending.danogo.oracles.deployments import (
    deployment_for_oracle_skh,
)
from charli3_dendrite.lending.danogo.oracles.forward import INTERMEDIATE_QUOTE
from charli3_dendrite.lending.danogo.oracles.forward import LiveLeaf
from charli3_dendrite.lending.danogo.oracles.forward import compose_cross_quote
from charli3_dendrite.lending.danogo.oracles.forward import leaf_discriminator
from charli3_dendrite.lending.danogo.oracles.forward import replay_recipe
from charli3_dendrite.lending.danogo.oracles.leaves import parse_liqwid_market_state
from charli3_dendrite.lending.danogo.oracles.leaves import parse_orcfax_fs
from charli3_dendrite.lending.danogo.oracles.locator import Recipe
from charli3_dendrite.lending.danogo.oracles.locator import load_registry
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.oracles.reproduce import leaf_forward_rates
from charli3_dendrite.lending.danogo.oracles.structured_datums import (
    StructuredOracleGlobalConfig,
)
from charli3_dendrite.lending.danogo.oracles.structured_datums import (
    StructuredOraclePathConfig,
)
from charli3_dendrite.lending.danogo.oracles.structured_datums import (
    StructuredOracleSource,
)
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import IncreaseLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import (
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.danogo.transactions.oracle_synth import (
    synthesize_oracle_redeemer,
)
from charli3_dendrite.lending.math import floor_div
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.lending.transactions.infra import PLACEHOLDER_FEE
from charli3_dendrite.lending.transactions.infra import additional_utxo_for_input
from charli3_dendrite.lending.transactions.infra import parse_out_ref
from charli3_dendrite.utility import asset_to_value
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    from charli3_dendrite.lending.danogo.oracles.locator import LeafHandle
    from charli3_dendrite.lending.danogo.transactions.context import Utxo

# Snapshots whose oracle helpers (price/leaf forwarding, config selection, alt-supply
# revaluation) read the same fields. Both create-loan and deposit/withdraw drive the
# oracle Withdraw, so its helpers accept either.
_OracleSnapshot: TypeAlias = (
    CreateLoanSnapshot
    | TopupWithdrawSnapshot
    | RepaySnapshot
    | IncreaseLoanSnapshot
    | ModifyCollateralSnapshot
)


def _value(lovelace: int, assets: list[tuple[str, str, int]]) -> Value:
    """Build a `Value` from a lovelace amount + (policy, name, qty) triples."""
    root: dict[str, int] = {"lovelace": lovelace}
    for policy, name, qty in assets:
        root[policy + name] = qty
    return asset_to_value(Assets(**root))


def _to_utxo(u: Utxo) -> UTxO:
    """Convert a resolved `Utxo` into a pycardano `UTxO` (inline datum + ref script)."""
    if u.out_ref is None:
        raise ValueError("cannot build a UTxO without an out-ref")
    tx_id_hex, out_idx = u.out_ref
    # Preserve the exact inline-datum bytes: these UTxOs are only ever referenced by
    # their out-ref (the datum is not re-serialized into the tx body), and some live
    # oracle source leaves carry a bare CBOR array datum that `RawPlutusData` rejects.
    datum = RawCBOR(bytes.fromhex(u.datum)) if u.datum else None
    script = PlutusV3Script(bytes.fromhex(u.ref_script)) if u.ref_script else None
    return UTxO(
        input=TransactionInput(
            transaction_id=TransactionId(bytes.fromhex(tx_id_hex)),
            index=out_idx,
        ),
        output=TransactionOutput(
            address=Address.decode(u.address),
            amount=_value(u.lovelace, u.assets),
            datum=datum,
            script=script,
        ),
    )


def _assets_with_lovelace(u: Utxo) -> tuple[tuple[str, str, int], ...]:
    """Leaf assets including the ADA balance as `("", "", coin)` (Splash-quote ADA)."""
    assets = tuple((p, n, q) for p, n, q in u.assets)
    if u.lovelace:
        assets = (*assets, ("", "", u.lovelace))
    return assets


def _leaf_for_handle(leaves: list[Utxo], handle: LeafHandle) -> Utxo | None:
    """Find the resolved source leaf a recipe step's handle points at.

    Matches by the handle's identifying NFT (policy + name) and, when present, the
    datum discriminator that pins kinds sharing one marker token across many feeds.
    """
    for u in leaves:
        if handle.nft_policy and not u.holds(handle.nft_policy, handle.nft_name):
            continue
        if (
            handle.datum_key
            and leaf_discriminator(handle.otype, u.datum) != handle.datum_key
        ):
            continue
        return u
    return None


def _resolve_recipe(
    recipe: Recipe,
    leaves: list[Utxo],
) -> tuple[list[tuple[LiveLeaf, bool, int]], list[tuple[Utxo, OracleUtxoType]]] | None:
    """Resolve a recipe's steps to live leaves + their ordered ``(leaf, otype)`` list.

    Returns the replay steps (for ``replay_recipe``) and the source leaves they read,
    or None if any step's leaf or oracle type cannot be resolved -- the collateral
    then stays unpriced rather than risk reading the wrong feed.
    """
    steps: list[tuple[LiveLeaf, bool, int]] = []
    resolved: list[tuple[Utxo, OracleUtxoType]] = []
    for step in recipe.steps:
        leaf = _leaf_for_handle(leaves, step.handle)
        if leaf is None or leaf.out_ref is None:
            return None
        try:
            otype = OracleUtxoType[step.handle.otype]
        except KeyError:
            return None
        steps.append(
            (
                LiveLeaf(
                    otype=step.handle.otype,
                    datum=leaf.datum,
                    assets=_assets_with_lovelace(leaf),
                ),
                step.is_reverse,
                step.scale_exp,
            ),
        )
        resolved.append((leaf, otype))
    return steps, resolved


def _compose_through_intermediate(
    quote: str,
    collat_steps: list[tuple[LiveLeaf, bool, int]],
    registry: dict[str, Recipe],
    leaves: list[Utxo],
) -> tuple[tuple[int, int], tuple[int, int], list[tuple[Utxo, OracleUtxoType]]] | None:
    """Price a cross-quote collateral through ADA, or None if it is not cross-quote.

    Resolves the canonical ``ada|quote`` intermediate recipe to this snapshot's source
    leaves, then delegates the cross-quote rate math to the shared `compose_cross_quote`
    (the one implementation both this builder path and the analytics resolver use).
    Returns the composed ``collateral -> quote`` rate, the intermediate ``ada -> quote``
    rate, and the intermediate recipe's source leaves (referenced by the redeemer); None
    leaves single-hop pricing unchanged.
    """
    intermediate_recipe = registry.get(f"{INTERMEDIATE_QUOTE}|{quote}")
    intermediate_steps: list[tuple[LiveLeaf, bool, int]] | None = None
    intermediate_leaves: list[tuple[Utxo, OracleUtxoType]] = []
    if intermediate_recipe is not None:
        resolved = _resolve_recipe(intermediate_recipe, leaves)
        if resolved is not None:
            intermediate_steps, intermediate_leaves = resolved
    composed = compose_cross_quote(quote, collat_steps, intermediate_steps)
    if composed is None:
        return None
    collat_price, intermediate_price = composed
    return collat_price, intermediate_price, intermediate_leaves


# External price-feed source kinds (Indigo / Liqwid-v2 / Djed): independent USD/ADA
# oracles the routing path config averages a cross-quote rate across and deviation-
# checks. Danogo pool / staking leaves are composition legs the recipe walk already
# collects, so they are never treated as cross-check feeds here.
_PRICE_FEED_OTYPES = (
    OracleUtxoType.TINDIGO,
    OracleUtxoType.TLIQWID_ORACLE_V2,
    OracleUtxoType.TDJED,
)

# Liquidity-pool source kinds a routing path config also averages the ``ada -> quote``
# rate across as an independent cross-check (e.g. a stablecoin/ADA Minswap pool standing
# in for an external feed). Unlike the price feeds these CAN also appear as recipe legs,
# so a candidate only qualifies when it is NOT already a referenced leg (the ``used``
# guard) and its rate lands within the path config's deviation tolerance.
_CROSS_CHECK_POOL_OTYPES = (OracleUtxoType.TMINSWAP_LP,)

_BPS = 10_000


def _classify_cross_check_leaf(leaf: Utxo) -> tuple[OracleUtxoType, Fraction] | None:
    """Read a cross-check source leaf's forward rate, or None if it is not one.

    Tries each independent price-feed parser first, then the liquidity-pool kinds a path
    config can average a cross-quote rate across; the first that yields a positive rate
    identifies the leaf's kind. A leaf that parses as none of them returns None, so only
    genuine alternative price sources qualify.
    """
    for otype in (*_PRICE_FEED_OTYPES, *_CROSS_CHECK_POOL_OTYPES):
        try:
            rates = leaf_forward_rates(
                OracleLeaf(
                    otype=otype,
                    datum=leaf.datum,
                    assets=_assets_with_lovelace(leaf),
                ),
            )
        except Exception:  # noqa: BLE001, S112 - a non-matching datum is not this kind
            continue
        if rates and rates[0][1] > 0:
            return otype, rates[0][1]
    return None


def _within_bps(rate: Fraction, target: Fraction, bps: int) -> bool:
    """True if ``rate`` is within ``bps`` basis points of the positive ``target``."""
    return abs(rate - target) * _BPS <= target * bps


def _quote_routing_deviation_bps(
    snapshot: _OracleSnapshot,
    *,
    supply_token: str,
) -> int | None:
    """The routing path config's deviation tolerance for the supply token, or None.

    The accepted routing config is the comprehensive one anchored to the supply
    token's routing anchor (the candidate carrying the most ``oracle_sources``; older
    versions are strict subsets). Its ``deviation_bps`` bounds how far an alternative
    price source may sit from the composed rate while still being averaged in.
    """
    anchor = ORACLE_QUOTE_ANCHOR.get(supply_token)
    if anchor is None:
        return None
    candidates = _anchored_path_configs(snapshot, anchor=anchor)
    if not candidates:
        return None
    _utxo, path = max(candidates, key=lambda c: len(c[1].oracle_sources))
    return path.deviation_bps


def _cross_quote_cross_check_leaves(
    snapshot: _OracleSnapshot,
    *,
    supply_token: str,
    intermediate_price: tuple[int, int],
    used: dict[tuple[str, int], tuple[Utxo, OracleUtxoType]],
) -> list[tuple[Utxo, OracleUtxoType]]:
    """The cross-check source leaves the path config routes the ada->quote rate through.

    A cross-quote market's ``ada -> quote`` conversion is averaged across the routing
    path config's alternative price sources and deviation-checked at its tolerance, so
    the oracle ``Withdraw`` references every such source leaf -- not only the single
    leg the mined recipe lists. A candidate qualifies when it is an independent
    cross-check source -- an external price-feed leaf or a liquidity-pool leaf -- not
    already referenced, whose forward rate (or its inverse) reproduces the composed
    ``ada -> quote`` rate within the path config's deviation tolerance. The leaf does
    not change the declared ``prices`` map; it is referenced because the validator
    walks it.
    """
    deviation_bps = _quote_routing_deviation_bps(snapshot, supply_token=supply_token)
    if deviation_bps is None:
        return []
    target = Fraction(*intermediate_price)
    if target <= 0:
        return []
    extra: list[tuple[Utxo, OracleUtxoType]] = []
    for leaf in snapshot.oracle_source_leaves:
        if leaf.out_ref is None or leaf.out_ref in used:
            continue
        classified = _classify_cross_check_leaf(leaf)
        if classified is None:
            continue
        otype, rate = classified
        if _within_bps(rate, target, deviation_bps) or _within_bps(
            1 / rate,
            target,
            deviation_bps,
        ):
            extra.append((leaf, otype))
    return extra


# Structured global-config source kinds → the leaf parser type. The newer deployment
# tags each source by a kind index that is NOT the `OracleUtxoType` value; only the
# kinds whose leaf rate is reproduced bit-exact (a single on-chain leaf pins the hop
# rate) are mapped. A source kind missing here (in a priced path) raises rather than
# guess a parser -- the price would be wrong. The absent kinds are those that compose
# several on-chain leaves into one rate, or that name no single on-chain token to pin a
# leaf to; a path routing through one of those is not forward-reproducible.
#
# Kind 1 is a Liqwid lending market: the hop rate is the market-STATE leaf's qToken
# exchange rate (a single leaf pins it), and the source ALSO names the market-PARAM
# leaf (field 1) -- a config record that does not enter the rate but is referenced
# alongside the state leaf, so the redeemer's source-leaf set carries both.
#
# Kind 0 (Orcfax, handled out of band -- not in this map) is the other two-leaf source:
# its rate is the named feed's Feed Status leaf exchange rate, read alongside the Feed
# Status Pointer leaf that points at it.
_STRUCTURED_KIND_OTYPE: dict[int, OracleUtxoType] = {
    1: OracleUtxoType.TLIQWID_MARKET_STATE,
    2: OracleUtxoType.TLIQWID_ORACLE_V2,
    3: OracleUtxoType.TDANOGO_POOL,
    4: OracleUtxoType.TINDIGO,
}

# Structured source kind whose hop reads a Liqwid market as a (STATE, PARAM) leaf pair.
_STRUCTURED_KIND_LIQWID_MARKET = 1

# Structured source kind for an Orcfax feed: its rate lives in a Feed Status leaf the
# source names by feed label, read alongside a Feed Status Pointer leaf (a bare
# bytestring pointing at the Feed Status NFT policy) the redeemer references but that
# carries no rate. Field 0 is the feed label; field 2 is an optional `[num, denom]`
# scale applied to the published rate.
_STRUCTURED_KIND_ORCFAX = 0
_ORCFAX_SCALE_FIELD = 2
_ORCFAX_TOKEN_PAIR = 2


def _leaf_yields_rate(leaf: Utxo, otype: OracleUtxoType) -> bool:
    """True if ``leaf`` parses as ``otype`` and yields a positive forward rate."""
    try:
        rates = leaf_forward_rates(
            OracleLeaf(
                otype=otype,
                datum=leaf.datum,
                assets=_assets_with_lovelace(leaf),
            ),
        )
    except (ValueError, KeyError, IndexError, TypeError):
        # A datum that does not parse as this kind is simply not this source.
        return False
    return bool(rates) and rates[0][1] > 0


def _single_leaf(matches: list[Utxo], source_index: int, kind: int) -> Utxo:
    """The sole snapshot leaf a structured source resolves to (else a hard error)."""
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one source leaf for structured oracle source "
            f"{source_index} (kind {kind}), found {len(matches)}",
        )
    return matches[0]


def _structured_hop_rate(leaf: Utxo, kind: int, otype: OracleUtxoType) -> Fraction:
    """The forward rate one structured hop's leaf contributes.

    A Liqwid market (kind 1) hop's rate is the market-STATE leaf's RAW qToken exchange
    rate (the underlying's own price is a separate hop in the structured path, so the
    cent-denominator the redeemer-driven reproduction folds in must NOT be applied
    here). Every other mapped kind takes the single forward rate its
    `leaf_forward_rates` parser yields. A leaf that yields no positive rate is a hard
    error.
    """
    if kind == _STRUCTURED_KIND_LIQWID_MARKET:
        if leaf.datum is None:
            raise ValueError("structured Liqwid market state leaf is missing its datum")
        num, denom = parse_liqwid_market_state(leaf.datum)
        return Fraction(num, denom)
    rates = leaf_forward_rates(
        OracleLeaf(otype=otype, datum=leaf.datum, assets=_assets_with_lovelace(leaf)),
    )
    if not rates or rates[0][1] <= 0:
        raise ValueError("structured oracle source leaf yielded no positive rate")
    return rates[0][1]


def _structured_source_leaf(
    snapshot: _OracleSnapshot,
    global_config: StructuredOracleGlobalConfig,
    source_index: int,
) -> tuple[Fraction, list[tuple[Utxo, OracleUtxoType]]]:
    """Resolve one structured path hop to its forward rate + referenced source leaves.

    The hop's ``source_index`` indexes the structured global config's source registry;
    its kind selects the leaf parser. Most sources carry a ``(policy, name)`` token
    locator that pins the rate-bearing leaf directly; a source whose locator names no
    token (an empty ``[b'', b'']`` -- e.g. the canonical ADA price feed) is pinned to
    the single snapshot leaf that parses as that kind. The hop rate comes from that one
    leaf (`_structured_hop_rate`).

    A Liqwid market (kind 1) additionally names its market-PARAM leaf in field 1; that
    leaf carries config (not the rate) but the on-chain validator reads it alongside
    the state leaf, so it joins the referenced set (typed ``TLIQWID_MARKET_PARAM``). The
    returned list is the leaves this hop reads, in the order the redeemer references
    them (state, then param). An out-of-range index, an unmapped kind, or no/multiple
    matching leaves is a hard error (the composed price would be wrong) so it raises
    rather than skip.
    """
    if not 0 <= source_index < len(global_config.sources):
        raise ValueError(
            f"structured oracle source index {source_index} is out of range",
        )
    source = global_config.sources[source_index]
    if source.kind == _STRUCTURED_KIND_ORCFAX:
        return _orcfax_hop(snapshot, source, source_index)
    otype = _STRUCTURED_KIND_OTYPE.get(source.kind)
    if otype is None:
        raise ValueError(
            f"unsupported structured oracle source kind {source.kind} "
            f"(source {source_index})",
        )
    locator = source.locator()
    if locator is not None and (locator[0] or locator[1]):
        policy, name = locator
        matches = [u for u in snapshot.oracle_source_leaves if u.holds(policy, name)]
    else:
        # No token locator: pin the source to the single snapshot leaf that parses as
        # this kind (e.g. the canonical ADA price feed an empty locator names).
        matches = [
            u for u in snapshot.oracle_source_leaves if _leaf_yields_rate(u, otype)
        ]
    leaf = _single_leaf(matches, source_index, source.kind)
    rate = _structured_hop_rate(leaf, source.kind, otype)
    referenced: list[tuple[Utxo, OracleUtxoType]] = [(leaf, otype)]
    if source.kind == _STRUCTURED_KIND_LIQWID_MARKET:
        referenced.append(
            (
                _market_param_leaf(snapshot, source, source_index),
                OracleUtxoType.TLIQWID_MARKET_PARAM,
            ),
        )
    return rate, referenced


def _market_param_leaf(
    snapshot: _OracleSnapshot,
    source: StructuredOracleSource,
    source_index: int,
) -> Utxo:
    """The Liqwid market-PARAM leaf a kind-1 structured source names in its field 1.

    The market-PARAM record is config (interest model / risk params), not a price, so it
    contributes no rate -- but the on-chain validator reads it together with the market
    state leaf, so the redeemer references it. Pinned by the field-1 ``(policy, name)``
    token; no/multiple matches is a hard error.
    """
    locator = source.token_at(1)
    if locator is None or not (locator[0] or locator[1]):
        raise ValueError(
            f"structured Liqwid market source {source_index} names no param leaf",
        )
    policy, name = locator
    matches = [u for u in snapshot.oracle_source_leaves if u.holds(policy, name)]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one market-param leaf for structured oracle source "
            f"{source_index}, found {len(matches)}",
        )
    return matches[0]


def _orcfax_scale(source: StructuredOracleSource) -> Fraction:
    """The optional ``[num, denom]`` scale an Orcfax source applies to its rate.

    Field 2 is a ``Constr0[num, denom]`` unit-conversion factor multiplied into the
    feed's published rate; a source without it (or with a zero denom) scales by 1.
    """
    if len(source.fields) <= _ORCFAX_SCALE_FIELD:
        return Fraction(1)
    field = source.fields[_ORCFAX_SCALE_FIELD]
    pair = field.value if isinstance(field, cbor2.CBORTag) else field
    if isinstance(pair, (list, tuple)) and len(pair) == _ORCFAX_TOKEN_PAIR:
        num, denom = int(pair[0]), int(pair[1])
        if denom:
            return Fraction(num, denom)
    return Fraction(1)


def _orcfax_fs_leaf(
    snapshot: _OracleSnapshot,
    feed_label: str,
    source_index: int,
) -> tuple[Utxo, tuple[int, int]]:
    """The Orcfax Feed Status leaf whose label matches, with its published rate.

    Pinned by the feed label its datum carries (e.g. ``CER/ADA-USDM/3``), the value the
    structured source names; no/multiple matches is a hard error.
    """
    matches: list[tuple[Utxo, tuple[int, int]]] = []
    for u in snapshot.oracle_source_leaves:
        if u.datum is None:
            continue
        try:
            label, rate = parse_orcfax_fs(u.datum)
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if label == feed_label:
            matches.append((u, rate))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one Orcfax feed-status leaf for structured oracle "
            f"source {source_index} ({feed_label!r}), found {len(matches)}",
        )
    return matches[0]


def _orcfax_fsp_leaf(
    snapshot: _OracleSnapshot,
    fs_leaf: Utxo,
    source_index: int,
) -> Utxo:
    """The Orcfax Feed Status Pointer leaf that points at ``fs_leaf``.

    Its inline datum is a bare bytestring equal to the Feed Status leaf's NFT policy, so
    it pins the pointer; the leaf carries no rate but the validator reads it alongside
    the status leaf, so the redeemer references it. No/multiple matches is a hard error.
    """
    fs_policies = {policy for policy, _name, _qty in fs_leaf.assets}
    matches: list[Utxo] = []
    for u in snapshot.oracle_source_leaves:
        if u.datum is None:
            continue
        try:
            value = cbor2.loads(bytes.fromhex(u.datum))
        except (ValueError, TypeError):
            continue
        if isinstance(value, (bytes, bytearray)) and value.hex() in fs_policies:
            matches.append(u)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one Orcfax feed-status-pointer leaf for structured "
            f"oracle source {source_index}, found {len(matches)}",
        )
    return matches[0]


def _orcfax_hop(
    snapshot: _OracleSnapshot,
    source: StructuredOracleSource,
    source_index: int,
) -> tuple[Fraction, list[tuple[Utxo, OracleUtxoType]]]:
    """Resolve an Orcfax (kind 0) hop to its rate + the (pointer, status) leaf pair.

    The source names a feed by label (field 0); the matching Feed Status leaf carries
    the published rate (scaled by the source's optional field-2 factor) and a single
    leaf pins it. The companion Feed Status Pointer leaf carries no rate but is read by
    the validator, so both join the referenced set (pointer first, then status, the
    order the redeemer references them).
    """
    feed = source.fields[0] if source.fields else None
    if not isinstance(feed, (bytes, bytearray)):
        raise ValueError(
            f"structured Orcfax source {source_index} names no feed label",
        )
    feed_label = bytes(feed).decode("ascii", "replace")
    fs_leaf, (num, denom) = _orcfax_fs_leaf(snapshot, feed_label, source_index)
    rate = Fraction(num, denom) * _orcfax_scale(source)
    if rate <= 0:
        raise ValueError(
            f"structured Orcfax source {source_index} yielded a non-positive rate",
        )
    fsp_leaf = _orcfax_fsp_leaf(snapshot, fs_leaf, source_index)
    return rate, [
        (fsp_leaf, OracleUtxoType.TORCFAX_FSP),
        (fs_leaf, OracleUtxoType.TORCFAX_FS),
    ]


def _walk_structured_path(
    snapshot: _OracleSnapshot,
    global_config: StructuredOracleGlobalConfig,
    path: tuple[tuple[int, bool], ...],
) -> tuple[tuple[int, int], list[tuple[Utxo, OracleUtxoType]]]:
    """Compose a priced unit's rate by walking its path's hops in order.

    Each hop's leaf forward rate is multiplied in, inverted when the hop is reversed
    (``is_reverse``); the exact-integer rational reduces to lowest terms, reproducing
    the on-chain price bit-for-bit. Returns the composed ``(num, denom)`` and the
    ordered ``(leaf, otype)`` the hops read (the order the redeemer references them).
    """
    price = Fraction(1)
    leaves: list[tuple[Utxo, OracleUtxoType]] = []
    for source_index, is_reverse in path:
        rate, hop_leaves = _structured_source_leaf(
            snapshot,
            global_config,
            source_index,
        )
        price *= (1 / rate) if is_reverse else rate
        leaves.extend(hop_leaves)
    if price <= 0:
        raise ValueError("structured oracle path composed a non-positive rate")
    return (price.numerator, price.denominator), leaves


def _structured_unit_paths(
    path_config: StructuredOraclePathConfig,
    unit: str,
) -> tuple[tuple[tuple[int, bool], ...], ...]:
    """The derivation path(s) the structured config prices ``unit`` through.

    The config's paths map keys each priceable token once, so the entry is unique (a
    unit with no entry, or more than one entry, is a hard error). A cross-quote unit
    may carry several alternative paths -- a primary plus deviation cross-checks the
    validator averages -- so all of them are returned in declaration order.
    """
    matches = [e for e in path_config.entries if e.unit == unit]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one structured path entry for {unit!r}, "
            f"found {len(matches)}",
        )
    return matches[0].paths


def _walk_structured_unit(
    snapshot: _OracleSnapshot,
    global_config: StructuredOracleGlobalConfig,
    paths: tuple[tuple[tuple[int, bool], ...], ...],
) -> tuple[tuple[int, int], list[tuple[Utxo, OracleUtxoType]]]:
    """Price a unit across its derivation paths; return the rate + referenced leaves.

    A unit's alternative paths are independent price sources the on-chain aggregator
    deviation-checks and reduces to a single declared rate: the MINIMUM of the resolved
    path rates (the conservative valuation). Every path that fully resolves contributes
    its leaves to the referenced set (deduped, in walk order), since the validator reads
    each path it averages -- not only the minimum one. A single-path unit reduces to
    exactly its one path. A path the forward resolver cannot pin to leaves (an
    unsupported source kind) is skipped; if it would have priced below the resolved
    minimum the declared rate is too high and the on-chain price calc rejects it (a loud
    failure, never a silent misprice). A unit no path resolves for raises.
    """
    if not paths:
        raise ValueError("structured path entry has no derivation paths")
    rates: list[Fraction] = []
    leaves: list[tuple[Utxo, OracleUtxoType]] = []
    seen: set[tuple[str, int]] = set()
    for path in paths:
        try:
            rate, path_leaves = _walk_structured_path(snapshot, global_config, path)
        except ValueError:
            continue
        rates.append(Fraction(*rate))
        for leaf, otype in path_leaves:
            assert leaf.out_ref is not None  # noqa: S101 - leaves carry an out-ref
            if leaf.out_ref not in seen:
                seen.add(leaf.out_ref)
                leaves.append((leaf, otype))
    if not rates:
        raise ValueError("no structured derivation path for the unit resolved")
    chosen = min(rates)
    return (chosen.numerator, chosen.denominator), leaves


def _structured_prices_and_leaves(
    snapshot: _OracleSnapshot,
    units: set[str] | None = None,
) -> tuple[dict[str, dict[str, tuple[int, int]]], list[tuple[Utxo, OracleUtxoType]]]:
    """Price collateral via the structured deployment's on-chain path config walk.

    The newer deployment stores no mined recipe: each priceable token's derivation
    path(s) live in the selected `StructuredOraclePathConfig`, with hops indexing the
    `StructuredOracleGlobalConfig` source registry. For each priced unit, walk its
    path(s) (`_walk_structured_unit`) -- the declared rate is the primary path's, while
    each alternative path that also resolves contributes its deviation cross-check
    leaves -- resolving each hop's source to its leaf + forward rate and composing them
    with exact-integer rationals (`_walk_structured_path`) to reproduce the on-chain
    ``prices`` map and the referenced source leaves. The resolver handles every source
    kind in use -- Orcfax (FS + FSP), Liqwid market (STATE + PARAM), Liqwid oracle V2,
    Danogo pool, and Indigo; a hop naming any other (unmapped) kind raises.

    A cross-quote market (supply token != ADA) additionally declares the ADA
    intermediate price first (matching the on-chain prices-map order), priced via the
    config's lovelace entry; its leaf is shared with the collateral walk. The returned
    leaf list is ordered as the redeemer references it -- the collateral path hops in
    order, then the intermediate -- de-duplicated by out-ref.
    """
    quote = snapshot.market_info.supply_token
    global_utxo = _select_global_config(snapshot)
    if global_utxo.datum is None:
        raise ValueError("structured oracle global config UTxO is missing its datum")
    global_config = StructuredOracleGlobalConfig.from_cbor(global_utxo.datum)
    path_utxo = _select_path_config(snapshot, supply_token=quote, used_leaves=[])
    if path_utxo.datum is None:
        raise ValueError("structured oracle path config UTxO is missing its datum")
    path_config = StructuredOraclePathConfig.from_cbor(path_utxo.datum)
    priceable = {e.unit for e in path_config.entries}

    targets = units if units is not None else set(snapshot.market_info.collaterals)

    prices: dict[str, dict[str, tuple[int, int]]] = {}
    used: dict[tuple[str, int], tuple[Utxo, OracleUtxoType]] = {}

    # Cross-quote markets price collateral through ADA; the on-chain redeemer declares
    # that ADA intermediate first, so it is added before the collateral entries.
    intermediate_leaves: list[tuple[Utxo, OracleUtxoType]] = []
    if quote != INTERMEDIATE_QUOTE and INTERMEDIATE_QUOTE in priceable:
        price, intermediate_leaves = _walk_structured_unit(
            snapshot,
            global_config,
            _structured_unit_paths(path_config, INTERMEDIATE_QUOTE),
        )
        prices.setdefault(quote, {})[INTERMEDIATE_QUOTE] = price

    for unit in sorted(targets):
        if unit == INTERMEDIATE_QUOTE:
            continue
        price, leaves = _walk_structured_unit(
            snapshot,
            global_config,
            _structured_unit_paths(path_config, unit),
        )
        prices.setdefault(quote, {})[unit] = price
        for leaf, otype in leaves:
            assert leaf.out_ref is not None  # noqa: S101 - leaves carry an out-ref
            used.setdefault(leaf.out_ref, (leaf, otype))

    for leaf, otype in intermediate_leaves:
        assert leaf.out_ref is not None  # noqa: S101 - leaves carry an out-ref
        used.setdefault(leaf.out_ref, (leaf, otype))

    return prices, list(used.values())


def _forward_prices_and_leaves(
    snapshot: _OracleSnapshot,
    units: set[str] | None = None,
) -> tuple[dict[str, dict[str, tuple[int, int]]], list[tuple[Utxo, OracleUtxoType]]]:
    """Recompute collateral prices forward + collect the source leaves they read.

    For each collateral with a packaged recipe, resolve each recipe step to one of the
    snapshot's source leaves and replay the recipe over the live leaf values (the same
    machinery `oracles.forward` uses). Returns the `prices` map keyed
    `quote -> {collateral: (num, denom)}` plus the de-duplicated ordered list of
    `(leaf, otype)` the prices read (used to build the redeemer's `oracle_idxs`).

    `units` restricts pricing to a specific collateral set (the loan's locked
    collateral). The transaction path passes the borrowed set so only its leaves are
    priced and referenced -- the on-chain create-loan shape prices exactly the loan's
    collateral, not every market collateral. When `units` is None all market
    collaterals with a recipe are priced (the read-only analytics path). A collateral
    that cannot be fully resolved is skipped, so prices are best-effort here.

    Dispatches on the snapshot's oracle deployment: the newer (structured) deployment
    walks its on-chain path config instead of the mined recipes
    (`_structured_prices_and_leaves`); the original (packed) deployment keeps the
    recipe-driven pricing below unchanged.
    """
    deployment = deployment_for_oracle_skh(snapshot.oracle_skh)
    if deployment.path_config_kind == "structured":
        return _structured_prices_and_leaves(snapshot, units=units)

    registry = load_registry()
    quote = snapshot.market_info.supply_token
    leaves = snapshot.oracle_source_leaves
    targets = units if units is not None else set(snapshot.market_info.collaterals)

    prices: dict[str, dict[str, tuple[int, int]]] = {}
    used: dict[tuple[str, int], tuple[Utxo, OracleUtxoType]] = {}
    for collateral in targets:
        recipe = registry.get(f"{collateral}|{quote}")
        # Intentional asymmetry vs the snapshot's leaf resolution (which raises on any
        # unresolved leaf): forward pricing here is lenient and skips a collateral it
        # cannot price, deferring "which collaterals MUST be priced" to the Ogmios eval.
        if recipe is None:
            continue
        resolved_recipe = _resolve_recipe(recipe, leaves)
        if resolved_recipe is None:
            continue
        steps, resolved = resolved_recipe
        if not steps:
            continue

        composed = _compose_through_intermediate(
            quote,
            steps,
            registry,
            leaves,
        )
        if composed is not None:
            collat_price, intermediate_price, intermediate_leaves = composed
            # The intermediate (ada) entry is declared first, matching the on-chain
            # prices-map order, then the composed collateral price.
            quote_prices = prices.setdefault(quote, {})
            quote_prices.setdefault(INTERMEDIATE_QUOTE, intermediate_price)
            quote_prices[collateral] = collat_price
            resolved = resolved + intermediate_leaves
        else:
            price = replay_recipe(steps, quote_unit=quote)
            if price is None:
                continue
            prices.setdefault(quote, {})[collateral] = price

        for leaf, otype in resolved:
            assert leaf.out_ref is not None  # noqa: S101 - narrowed above
            used.setdefault(leaf.out_ref, (leaf, otype))

    # Cross-quote markets price collateral through an ``ada -> quote`` intermediate the
    # routing path config averages across several price sources. The recipe walk above
    # captures one leg, but the oracle Withdraw references every alternative source it
    # deviation-checks the rate against, so add those cross-check leaves (they do not
    # alter the prices map). Single-hop / ADA-quote markets declare no intermediate, so
    # nothing is added.
    intermediate = prices.get(quote, {}).get(INTERMEDIATE_QUOTE)
    if intermediate is not None:
        for leaf, otype in _cross_quote_cross_check_leaves(
            snapshot,
            supply_token=quote,
            intermediate_price=intermediate,
            used=used,
        ):
            assert leaf.out_ref is not None  # noqa: S101 - narrowed in the helper
            used.setdefault(leaf.out_ref, (leaf, otype))
    return prices, list(used.values())


def _ref_index(tx_builder: TransactionBuilder) -> dict[tuple[str, int], int]:
    """Map each reference input's out-ref to its index in the canonical ordering.

    The Plutus script context exposes reference inputs sorted by output reference
    (transaction id bytes, then output index), so indices the redeemers carry are
    derived from that sort -- not the builder's (hash-ordered) set iteration.
    """
    inputs = [
        utxo.input for utxo in tx_builder.reference_inputs if isinstance(utxo, UTxO)
    ]
    inputs.sort(key=lambda i: (bytes(i.transaction_id), i.index))
    return {
        (bytes(i.transaction_id).hex(), i.index): pos for pos, i in enumerate(inputs)
    }


def _set_validity_window(
    tx_builder: TransactionBuilder,
    *,
    slots: int,
    txn_time: int | None,
) -> int:
    """Set the builder's validity window and resolve the datum-synthesis time.

    The window starts at the builder's existing `validity_start` (or the chain
    context's current slot) and runs `slots` slots; `ttl` is set to the upper bound.
    Returns the POSIX-millisecond transaction time used for datum synthesis: the
    caller's `txn_time` when given, else derived from the lower bound.
    """
    lower = (
        tx_builder.validity_start
        if tx_builder.validity_start is not None
        else tx_builder.context.last_block_slot
    )
    tx_builder.validity_start = lower
    tx_builder.ttl = lower + slots
    return slot_to_posix_ms(lower) if txn_time is None else txn_time


def _select_global_config(snapshot: _OracleSnapshot) -> Utxo:
    """The single global source config the oracle Withdraw redeemer points at.

    Identified by the deployment's Oracle Global Config NFT (one UTxO holds it), not by
    a loose datum parse -- both the packed-bytes and structured shapes also match
    unrelated datums. The deployment (and so its global-config NFT) is resolved from
    the snapshot's `oracle_skh`, so each deployment selects its own config UTxO. The
    redeemer's `oracle_source_idx` references this UTxO.
    """
    deployment = deployment_for_oracle_skh(snapshot.oracle_skh)
    nft = deployment.global_config_nft
    policy, name = nft[:56], nft[56:]
    matches = [u for u in snapshot.oracle_data_refs if u.holds(policy, name)]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one oracle global config UTxO, found {len(matches)}",
        )
    return matches[0]


_LP_NAME_HEX_LEN = 64  # 32-byte Minswap V2 LP token name, hex-encoded


def _minswap_lp_names(used_leaves: list[tuple[Utxo, OracleUtxoType]]) -> set[str]:
    """Minswap V2 LP token names (32-byte, hex) among the borrowed-collateral leaves.

    These names appear verbatim as Minswap (`tag 7`) entries in the routing path
    config's `oracle_sources`, so they pin the one path config that prices the loan's
    collateral.
    """
    names: set[str] = set()
    for leaf, _otype in used_leaves:
        for policy, name, _qty in leaf.assets:
            if policy == MINSWAP_LP_POLICY and len(name) == _LP_NAME_HEX_LEN:
                names.add(name)
    return names


def _anchored_path_configs(
    snapshot: _OracleSnapshot,
    *,
    anchor: str,
) -> list[tuple[Utxo, OraclePathDatum]]:
    """The path-config UTxOs whose datum anchors to `anchor`, with the parsed datum.

    Many path-config UTxOs share the path FT policy (older config versions, other
    supply tokens); only those whose `OraclePathDatum.anchor` is the supply token's
    routing anchor are candidates for that token's pricing.
    """
    out: list[tuple[Utxo, OraclePathDatum]] = []
    for u in snapshot.oracle_data_refs:
        if u.datum is None:
            continue
        if not any(policy == ORACLE_PATH_FT_POLICY for policy, _n, _q in u.assets):
            continue
        try:
            path = OraclePathDatum.from_cbor(u.datum)
        except Exception:  # noqa: BLE001, S112 - skip non-path datums (e.g. config)
            continue
        if path.anchor.hex() == anchor:
            out.append((u, path))
    return out


def _select_path_config(
    snapshot: _OracleSnapshot,
    *,
    supply_token: str,
    used_leaves: list[tuple[Utxo, OracleUtxoType]],
) -> Utxo:
    """The single routing-path config the oracle Withdraw validator accepts.

    Dispatches on the snapshot's deployment (resolved from its `oracle_skh`):

    - The structured deployment stores one path-config UTxO per supply token, so the
      accepted config is the one whose structured datum's supply-token tuple equals the
      market's supply token (`_select_structured_path_config`).
    - The packed deployment selects among the path datums anchored to the supply
      token's routing anchor (`_select_packed_path_config`).

    The redeemer's `oracle_path_idxs` references the chosen UTxO.
    """
    deployment = deployment_for_oracle_skh(snapshot.oracle_skh)
    if deployment.path_selection == "supply_token":
        return _select_structured_path_config(
            snapshot,
            deployment=deployment,
            supply_token=supply_token,
        )
    return _select_packed_path_config(
        snapshot,
        supply_token=supply_token,
        used_leaves=used_leaves,
    )


def _select_structured_path_config(
    snapshot: _OracleSnapshot,
    *,
    deployment: OracleDeployment,
    supply_token: str,
) -> Utxo:
    """The structured deployment's routing-path config for `supply_token`.

    The structured deployment publishes one path-config UTxO per supply token (all
    sharing the deployment's path-config mint policy); the accepted config is the one
    whose `StructuredOraclePathConfig.supply_unit` equals the market's supply token.
    The global config shares the mint policy but is not a path config (its datum has no
    paths map), so it fails to parse here and is skipped.
    """
    matches: list[Utxo] = []
    for u in snapshot.oracle_data_refs:
        if u.datum is None:
            continue
        if not any(p == deployment.path_config_policy for p, _n, _q in u.assets):
            continue
        try:
            config = StructuredOraclePathConfig.from_cbor(u.datum)
        except Exception:  # noqa: BLE001, S112 - skip non-path-config datums
            continue
        if config.supply_unit == supply_token:
            matches.append(u)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one structured routing-path config for supply token "
            f"{supply_token!r}, found {len(matches)}",
        )
    return matches[0]


def _select_packed_path_config(
    snapshot: _OracleSnapshot,
    *,
    supply_token: str,
    used_leaves: list[tuple[Utxo, OracleUtxoType]],
) -> Utxo:
    """The packed deployment's routing-path config the oracle Withdraw accepts.

    Candidates are the path datums anchored to the supply token's routing anchor.
    Among those, the disambiguation depends on what the action prices:

    - When a priced leaf is a Minswap LP (the create-loan collateral path), the
      borrowed collateral's Minswap LP token names appear verbatim as the config's
      Minswap sources, so the config whose `oracle_sources` cover them is unique.
    - When nothing priced is a Minswap LP (an alt-supply deposit/withdraw re-prices
      only the alternative supply token, sourced from a Danogo/Splash pool whose leaf
      carries no Minswap LP token), the routing config cannot be told apart by source
      coverage. The accepted config is the comprehensive current routing registry --
      the anchored config with the most `oracle_sources` (older/stub versions carry a
      strict subset) -- so the candidate with the largest source set is taken.
    """
    if ORACLE_PATH_FT_POLICY is None:
        raise ValueError("ORACLE_PATH_FT_POLICY anchor not set")
    anchor = ORACLE_QUOTE_ANCHOR.get(supply_token)
    if anchor is None:
        raise ValueError(f"no oracle routing anchor for supply token {supply_token!r}")
    candidates = _anchored_path_configs(snapshot, anchor=anchor)

    wanted = _minswap_lp_names(used_leaves)
    if wanted:
        matches = [
            u
            for u, path in candidates
            if wanted <= {source.payload.hex() for source in path.decode_sources()}
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one routing-path config covering the collateral, "
                f"found {len(matches)}",
            )
        return matches[0]

    if not candidates:
        raise ValueError(
            f"no oracle routing-path config anchored to {anchor} for supply token "
            f"{supply_token!r}",
        )
    most = max(len(path.oracle_sources) for _u, path in candidates)
    largest = [u for u, path in candidates if len(path.oracle_sources) == most]
    if len(largest) != 1:
        raise ValueError(
            f"cannot disambiguate the routing-path config: {len(largest)} configs "
            f"tie at {most} sources",
        )
    return largest[0]


def _pool_output_value(pool: Utxo, supply_token: str, supply_delta: int) -> Value:
    """The updated pool value: the spent pool's bag with the supply token adjusted.

    `supply_delta` is the signed change to the pool's supply-token holding -- negative
    for an outflow (a borrow draws the supply token out; a withdrawal returns it to the
    actor) and positive for a deposit (a top-up adds supply token to the pool).
    """
    root: dict[str, int] = {"lovelace": pool.lovelace}
    for policy, name, qty in pool.assets:
        root[policy + name] = qty
    key = "lovelace" if supply_token == "lovelace" else supply_token
    root[key] = root.get(key, 0) + supply_delta
    return asset_to_value(Assets(**root))


def build_pool_script_output(
    pool: Utxo,
    *,
    supply_token: str,
    supply_delta: int,
    datum: PoolDatum,
) -> TransactionOutput:
    """Build the recreated pool script output with its supply delta + min-UTxO floor.

    The value is the spent pool's bag with the supply token adjusted by
    ``supply_delta`` (`_pool_output_value`), carrying the synthesized ``datum``. The
    lovelace coin is then floored to ``OUTPUT_MIN_ADA``: `_pool_output_value` already
    applies the supply delta to the pool's holding -- including ADA itself for a
    lovelace-supply pool (where the supply token IS the coin) -- so the floor only
    raises a sub-minimum balance, never resets it to the pre-action balance (which
    would drop a lovelace deposit/withdrawal). For a native-token-supply pool the coin
    is unchanged (just the min-ADA balance), so this is identical to flooring the
    pool's prior lovelace.
    """
    pool_out = TransactionOutput(
        address=Address.decode(pool.address),
        amount=_pool_output_value(pool, supply_token, supply_delta),
        datum=datum,
    )
    pool_out.amount.coin = max(pool_out.amount.coin, OUTPUT_MIN_ADA)
    return pool_out


# Market Param datum field index of the protocol fee-recipient (treasury) address.
_MARKET_FEE_ADDRESS_FIELD = 8

# Plutus `Constr` CBOR tags for the two credential variants (key vs script).
_CONSTR_VKEY = 121
_CONSTR_SCRIPT = 122


def _credential(constr: object) -> VerificationKeyHash | ScriptHash:
    """A Plutus credential `Constr(tag, [hash])` -> pycardano key/script hash."""
    tag = getattr(constr, "tag", _CONSTR_VKEY)
    payload = constr.value[0]  # type: ignore[attr-defined]
    if tag == _CONSTR_SCRIPT:
        return ScriptHash(payload)
    return VerificationKeyHash(payload)


def _market_fee_address(market_datum: str) -> Address:
    """Decode the protocol fee-recipient address from the Market Param datum.

    Field 8 is a Plutus `Address` (`Constr0[payment_credential, Option<stake>]`); the
    create-loan fee output must be paid here. Both credentials may be key- or
    script-based; the stake reference is the inline `StakingHash` form (no pointers).
    """
    fields = RawPlutusData.from_cbor(market_datum).data.value
    addr = fields[_MARKET_FEE_ADDRESS_FIELD]
    payment = _credential(addr.value[0])
    stake_opt = addr.value[1]
    staking: VerificationKeyHash | ScriptHash | None = None
    if (
        getattr(stake_opt, "tag", _CONSTR_SCRIPT) == _CONSTR_VKEY
    ):  # Some(StakingHash(c))
        staking = _credential(stake_opt.value[0].value[0])
    return Address(payment_part=payment, staking_part=staking, network=Network.MAINNET)


def _pool_holding(pool: Utxo, unit: str) -> int:
    """Quantity of `unit` the pool UTxO holds (its lovelace balance for ADA)."""
    if unit == "lovelace":
        return pool.lovelace
    policy, name = unit[:56], unit[56:]
    for asset_policy, asset_name, qty in pool.assets:
        if asset_policy == policy and asset_name == name:
            return qty
    return 0


def _alt_supply_update(
    snapshot: _OracleSnapshot,
    prices: dict[str, dict[str, tuple[int, int]]],
) -> tuple[int, list[PRational] | None]:
    """Revalue the pool's alt-supply holdings at the live oracle prices.

    The create-loan validator re-prices each alternative supply token the oracle
    redeemer carries a price for and writes that price into the pool datum's
    ``alt_supply_tokens_rate``, booking the change in the holdings' supply-token value
    as interest: ``alt_tokens_interest = sum(floor(amount * (new_rate - old_rate)))``.

    An alt-supply token the oracle does not price -- one with no on-chain pricing
    recipe (a disallowed token the pool never accepts, which is consequently never
    held) -- is NOT re-priced: the validator carries its prior rate unchanged, and it
    contributes no interest (its holding is zero, so ``floor(0 * (new - old)) == 0``
    regardless of price). Reproducing that, a token absent from the live ``prices`` map
    keeps its prior rate and adds nothing, so a pool listing such a token still builds.

    Returns the summed ``alt_tokens_interest`` and the new per-token rate list (its
    order matches the market's alt-supply tokens, which is the prior datum's rate-list
    order). For a pool with no alt tokens it returns ``(0, None)`` so the rate list is
    carried through unchanged.
    """
    market = snapshot.market_info
    if not market.alt_supply_tokens or snapshot.pool.datum is None:
        return 0, None
    quote_prices = prices.get(market.supply_token, {})
    prev_rates = PoolDatum.from_cbor(snapshot.pool.datum).alt_supply_tokens_rate
    if len(prev_rates) != len(market.alt_supply_tokens):
        # Unexpected datum shape: carry the rate list through rather than corrupt it.
        return 0, None

    alt_interest = 0
    new_rates: list[PRational] = []
    for prev_rate, alt_unit in zip(prev_rates, market.alt_supply_tokens):
        price = quote_prices.get(alt_unit)
        if price is None:
            # No live oracle price: the validator carries the prior rate unchanged and
            # the (zero-held) token contributes no interest. Keep the rate-list order
            # and length aligned to the prior datum.
            new_rates.append(PRational(num=prev_rate.num, denom=prev_rate.denom))
            continue
        new_num, new_denom = price
        amount = _pool_holding(snapshot.pool, alt_unit)
        # floor(amount * (new_num/new_denom - old_num/old_denom)) as one floored ratio.
        delta_num = new_num * prev_rate.denom - prev_rate.num * new_denom
        alt_interest += floor_div(amount * delta_num, new_denom * prev_rate.denom)
        new_rates.append(PRational(num=new_num, denom=new_denom))
    return alt_interest, new_rates


def prepare_oracle_withdraw(
    snapshot: _OracleSnapshot,
    *,
    price_units: set[str],
    supply_token: str,
    revalue_alt_supply: bool,
) -> tuple[
    OraclePriceCalcRdmr,
    Utxo,
    Utxo,
    list[tuple[Utxo, OracleUtxoType]],
    int,
    list[PRational] | None,
]:
    """Resolve the oracle Withdraw wiring shared by every action that drives it.

    Forward-prices ``price_units`` to the live ``prices`` map + source leaves
    (`_forward_prices_and_leaves`), selects the single global-source and routing-path
    configs the validator accepts (`_select_global_config` / `_select_path_config`), and
    seeds the price-calc redeemer (its ref-input indices + priced-leaf list are filled
    later from the FINAL builder ordering by `_synth_oracle_redeemer`). Each action
    decides which units it prices (its locked collateral and/or the market's alt-supply
    tokens) and passes them as ``price_units``.

    ``revalue_alt_supply`` is the one piece that differs between actions and is NOT
    collapsed: when True the pool's alternative supply-token holdings are re-priced from
    the live oracle and the change booked as interest (`_alt_supply_update`) -- the
    full revaluation create-loan / increase-loan / a deposit top-up performs; when False
    the prior per-token rates are carried through and zero interest is booked -- repay
    (which re-prices the held alt tokens to the rate they already carry) and actions
    that do not revalue the pool (modify-collateral, which spends no pool and discards
    these). Returns the seeded redeemer, the chosen global + path configs, the priced
    source leaves, and the alt revaluation (``alt_tokens_interest`` + new per-token
    rates).
    """
    prices, used_leaves = _forward_prices_and_leaves(snapshot, price_units)
    global_config = _select_global_config(snapshot)
    path_config = _select_path_config(
        snapshot,
        supply_token=supply_token,
        used_leaves=used_leaves,
    )
    if revalue_alt_supply:
        alt_tokens_interest, new_alt_rates = _alt_supply_update(snapshot, prices)
    else:
        alt_tokens_interest, new_alt_rates = 0, None
    oracle_rdmr = OraclePriceCalcRdmr(
        oracle_source_idx=0,
        oracle_path_idxs=[],
        oracle_idxs=[],
        prices=prices,
        borrow_rates={},
    )
    return (
        oracle_rdmr,
        global_config,
        path_config,
        used_leaves,
        alt_tokens_interest,
        new_alt_rates,
    )


def attach_oracle_withdraw(
    tx_builder: TransactionBuilder,
    snapshot: _OracleSnapshot,
    *,
    oracle_rdmr: OraclePriceCalcRdmr,
    hub: tuple[Utxo, PlutusData, str] | None = None,
) -> None:
    """Attach the oracle Withdraw price calc + an optional delegation-hub withdrawal.

    Every action that drives the oracle runs it as a zero withdrawal against the oracle
    reward address (``f1 + oracle_skh``). Repay/increase additionally delegate through a
    hub withdrawal -- a zero withdrawal against the loan- resp. pool-script reward
    address carrying the action redeemer -- added BEFORE the oracle withdrawal, so the
    reward accounts register hub-first then oracle; deposit/withdraw and
    modify-collateral drive the oracle alone (no hub). The ``hub`` tuple, when given,
    carries that hub's ``(reference script, redeemer, script hash)``.
    """
    if snapshot.oracle_script_ref is None:
        raise ValueError("market is missing its resolved oracle reference script")
    withdrawals: dict[bytes, int] = {}
    if hub is not None:
        hub_script_ref, hub_redeemer, hub_skh = hub
        tx_builder.add_withdrawal_script(
            _to_utxo(hub_script_ref),
            Redeemer(hub_redeemer),
        )
        hub_reward_addr = Address(
            staking_part=ScriptHash(bytes.fromhex(hub_skh)),
            network=Network.MAINNET,
        )
        withdrawals[bytes(hub_reward_addr)] = 0
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.oracle_script_ref),
        Redeemer(oracle_rdmr),
    )
    oracle_reward_addr = Address(
        staking_part=ScriptHash(bytes.fromhex(snapshot.oracle_skh)),
        network=Network.MAINNET,
    )
    withdrawals[bytes(oracle_reward_addr)] = 0
    tx_builder.withdrawals = Withdrawals(withdrawals)


def _synth_oracle_redeemer(
    tx_builder: TransactionBuilder,
    *,
    oracle_rdmr: OraclePriceCalcRdmr,
    used_leaves: list[tuple[Utxo, OracleUtxoType]],
    prices: dict[str, dict[str, tuple[int, int]]],
    global_config: Utxo,
    path_config: Utxo,
    preserve_leaf_order: bool = False,
) -> None:
    """Fill the oracle redeemer's ref-input indices + priced leaves from the ordering.

    Shared by both pool actions that drive the oracle Withdraw (create-loan and
    alt-supply deposit/withdraw): the global-source and routing-path config indices
    and every priced source leaf's index come from the canonical out-ref sort the
    Plutus script context exposes, so this must run after all reference inputs are
    added. Mutates `oracle_rdmr` in place.

    `preserve_leaf_order` keeps the `used_leaves` order in ``oracle_idxs`` rather than
    sorting by reference-input index: the structured deployment references its priced
    leaves in path-walk order (not sorted), so its redeemer reproduces byte-exact only
    when that order is preserved. The packed deployment leaves the default (sorted).
    """
    ref_index = _ref_index(tx_builder)
    oracle_leaf_tuples: list[tuple[UTxOTarget, OracleUtxoType, int, OracleLeaf]] = []
    for leaf, otype in used_leaves:
        if leaf.out_ref is None:
            continue
        oracle_leaf_tuples.append(
            (
                UTxOTarget.REF,
                otype,
                ref_index[leaf.out_ref],
                OracleLeaf(
                    otype=otype,
                    datum=leaf.datum,
                    assets=_assets_with_lovelace(leaf),
                ),
            ),
        )
    if not preserve_leaf_order:
        oracle_leaf_tuples.sort(key=lambda t: t[2])

    if global_config.out_ref is None or path_config.out_ref is None:
        raise ValueError("oracle global/path config UTxO is missing its out-ref")
    source_idx = ref_index[global_config.out_ref]
    path_idxs = [ref_index[path_config.out_ref]]

    synthesized = synthesize_oracle_redeemer(
        leaves=oracle_leaf_tuples,
        prices=prices,
        borrow_rates={},
        oracle_source_idx=source_idx,
        oracle_path_idxs=path_idxs,
    )
    oracle_rdmr.oracle_source_idx = synthesized.oracle_source_idx
    oracle_rdmr.oracle_path_idxs = synthesized.oracle_path_idxs
    oracle_rdmr.oracle_idxs = synthesized.oracle_idxs
    oracle_rdmr.prices = synthesized.prices
    oracle_rdmr.borrow_rates = synthesized.borrow_rates


def add_actor_funding(
    tx_builder: TransactionBuilder,
    *,
    actor: Address,
    actor_utxo: str,
    funding: dict[str, int],
) -> dict[str, Any]:
    """Add the actor funding input for a deposit/withdraw + placeholder fee.

    The actor supplies the action's tokens: the deposited supply token (deposit) or
    the dTokens to burn (withdraw). Its value is resolved live by Ogmios, so a nominal
    output value is enough here (manual assembly serializes only the out-ref).

    Returns the Ogmios `additionalUtxo` entry for the actor input: every other Danogo
    input/reference is a freshly resolved live UTxO Ogmios resolves from its own
    ledger, so only this funding input -- a plain wallet UTxO that may already be spent
    -- must be supplied explicitly.
    """
    actor_input = parse_out_ref(actor_utxo)
    actor_value = dict(funding)
    actor_value["lovelace"] = max(actor_value.get("lovelace", 0), PLACEHOLDER_FEE)
    actor_funding_utxo = UTxO(
        input=actor_input,
        output=TransactionOutput(actor, asset_to_value(Assets(**actor_value))),
    )
    tx_builder.add_input(actor_funding_utxo)
    tx_builder.fee = PLACEHOLDER_FEE
    return additional_utxo_for_input(actor_utxo, str(actor), funding)


def collateral_health_value(
    snapshot: _OracleSnapshot,
    collateral_map: dict[str, int],
) -> int:
    """The threshold-weighted oracle value of a collateral set (the loan's HF headroom).

    Each unit in ``collateral_map`` is priced via the live oracle recipes
    (`_forward_prices_and_leaves`) in the market's supply-token quote and weighted by
    the market liquidation threshold (bps), summed by
    `total_collateral_val_with_threshold`.
    A unit with no resolvable oracle price, or one the market does not accept (threshold
    <= 0), is a hard error -- it would misprice the loan -- so it raises rather than
    skip. Returns the threshold-weighted value the ``safe_*`` preflights compare against
    the loan amount.
    """
    market = snapshot.market_info
    quote = market.supply_token
    prices, _ = _forward_prices_and_leaves(snapshot, set(collateral_map))
    quote_prices = prices.get(quote, {})

    terms: list[tuple[int, int, int, int]] = []
    for unit, qty in collateral_map.items():
        price = quote_prices.get(unit)
        if price is None:
            raise ValueError(
                f"no live oracle price for collateral {unit!r} (quote {quote!r})",
            )
        threshold = market.threshold_for(unit)
        if threshold <= 0:
            raise ValueError(f"collateral {unit!r} is not accepted by the market")
        num, denom = price
        terms.append((qty, num, denom, threshold))

    return total_collateral_val_with_threshold(terms)
