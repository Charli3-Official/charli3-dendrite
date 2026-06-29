"""Leaf-source price extractors for the Danogo oracle aggregator.

This module holds the pure per-leaf price arithmetic the aggregation engine depends
on. Each function maps a leaf oracle source's confirmed datum fields to a
``(num, denom)`` rate, matching the on-chain ``handle_oracle_idx`` per-type logic
documented in Danogo's oracle-aggregator audit.

Verified bit-exact against on-chain ``OraclePriceCalcRdmr`` prices (tx
929a7d68...de8237 and 590d...e6a069):
* ``danogo_pool_dtoken_rate`` (``TDANOGO_POOL``).
* ``parse_danogo_staking_rate`` (``TDANOGO_STAKING``).
* ``parse_liqwid_oracle_v2`` (``TLIQWID_ORACLE_V2``) -- reproduces ADA = 16271/100000.
* ``parse_minswap_lp`` + ``cpamm_price_from_reserves`` (``TMINSWAP_LP``) -- the
  STRIKE/ADA hop of a 3-hop derived price.
* ``parse_djed`` (``TDJED``) -- pair rate decoded.

* ``parse_splash_cpamm_g3`` + ``splash_lp_token_price`` (``TSPLASH_CPAMM_G3``) --
  Splash LP-token collateral priced at 2*reserve_quote/circulating_lp; verified
  bit-exact across all 29 on-chain LP-token pricings.

Value parsers landed; bit-exact end-to-end check pending the ``price_path`` decode
(these source types only ever appear as intermediate hops on-chain, never as a
standalone collateral price):
* ``parse_indigo`` (``TINDIGO``) -- iAsset->ADA, 1e6 scale.
* ``parse_liqwid_market_state`` (``TLIQWID_MARKET_STATE``) -- qToken exchange rate;
  reproduces the on-chain qToken price as ``rate/100/ada_usd`` for the one market
  that appears on-chain.

Every ``OracleUtxoType`` observed in full mainnet history (9 of the 17 registry
entries) now has a parser, including ``TLIQWID_MARKET_PARAM`` (a config record, not a
price). Not implemented (never seen on-chain): ``TORCFAX_FSP/FS``,
``TLIQWID_ORACLE_V1``, ``TSPLASH_CPAMM_G1/G2``, ``TSPLASH_STABLE``, ``TCHARLI3``,
``TMINSWAP_LP_STABLE``.
"""

from __future__ import annotations

import cbor2  # type: ignore[import-not-found]

from charli3_dendrite.lending.danogo.datums import asset_unit

# Liqwid Oracle V2 datum field order: [feed_id, Constr1[[asset]], [num, denom],
# timestamp_ms, validity_ms, config].
_LIQWID_ASSET_FIELD = 1
_LIQWID_PRICE_FIELD = 2
# Minswap V2 pool datum field order: [stake, asset_a, asset_b, total_liquidity,
# reserve_a, reserve_b, fee_a, fee_b, ...].
_MS_ASSET_A_FIELD = 1
_MS_ASSET_B_FIELD = 2
_MS_RESERVE_A_FIELD = 4
_MS_RESERVE_B_FIELD = 5
# Indigo oracle datum: Constr0[Constr0[price_int], timestamp]; price is ADA-per-iAsset
# scaled by 1e6 (Indigo's fixed convention).
_INDIGO_SCALE = 1_000_000
# Splash CPAMM (G3) pool datum: [pool_nft, asset_x, asset_y, asset_lq, ...]. The LP
# token is minted from a fixed cap of 2**63 - 1; circulating LP = cap - the balance
# the pool still holds.
_SPLASH_ASSET_X_FIELD = 1
_SPLASH_ASSET_Y_FIELD = 2
_SPLASH_LP_FIELD = 3
_SPLASH_MAX_LP = (1 << 63) - 1
# Liqwid market-state datum: the qToken<->underlying exchange rate is the rational at
# index 9 of the 11-field state record.
_LIQWID_MARKET_RATE_FIELD = 9
# Liqwid market-parameter (config) datum: interest-model + risk fields. Only a couple
# carry unambiguous values; the leaf is not a price source.
_LIQWID_PARAM_BASE_RATE_FIELD = 0
_LIQWID_PARAM_COLLATERAL_RATIO_FIELD = 12


def _fields(obj: object) -> list:
    """Positional fields of a CBOR Constr (or a bare list) as a Python list."""
    inner = obj.value if isinstance(obj, cbor2.CBORTag) else obj
    return list(inner)


def _tuple_asset_unit(arr: object) -> str:
    """Dendrite unit string from a ``[policy, name]`` value (Constr-wrapped or bare)."""
    seq = _fields(arr)
    policy = seq[0] if len(seq) > 0 and isinstance(seq[0], bytes) else b""
    name = seq[1] if len(seq) > 1 and isinstance(seq[1], bytes) else b""
    return asset_unit(policy, name)


def cpamm_price_from_reserves(
    *,
    reserve_token: int,
    reserve_quote: int,
) -> tuple[int, int]:
    """Constant-product spot price of token in quote = reserve_quote / reserve_token.

    Returns (num, denom). Raises ValueError on non-positive reserves (Minswap/Splash).
    """
    if reserve_token <= 0 or reserve_quote <= 0:
        raise ValueError("non-positive reserve")
    return (reserve_quote, reserve_token)


def danogo_pool_dtoken_rate(
    *,
    total_supply: int,
    circulating_dtoken: int,
) -> tuple[int, int]:
    """Danogo dToken -> underlying rate = total_supply / circulating_dtoken.

    The ``TDANOGO_POOL`` leaf: a Danogo lending pool's dToken accrues value as
    interest grows ``total_supply`` (underlying held + lent) relative to the
    fixed ``circulating_dtoken`` in holders' hands. Returns (num, denom).

    Verified bit-exact against an on-chain ``OraclePriceCalcRdmr`` price: a pool
    with total_supply=557273970678, circulating_dtoken=562160462680 yields
    278636985339/281080231340 (tx 590d...e6a069, leaf ``(OUT, TDANOGO_POOL, 0)``).

    Raises ValueError on non-positive supply (a pool with no circulating dTokens
    has no defined rate).
    """
    if total_supply <= 0 or circulating_dtoken <= 0:
        raise ValueError("non-positive pool supply")
    return (total_supply, circulating_dtoken)


def parse_danogo_staking_rate(datum: str | bytes) -> tuple[int, int]:
    """Danogo staking leaf -> staked-token rate = field0 / field1.

    Datum is ``Constr0[total_staked, circulating, timestamp]`` -- the staking
    analogue of the lending pool: the staked token accrues value as ``total_staked``
    grows against the fixed ``circulating`` supply. Returns (num, denom).

    Verified bit-exact: a leaf with (15013620167, 14750722364, ts) yields the
    on-chain price 2144802881/2107246052 (tx ad5ec9af...5ec862, leaf
    ``(OUT, TDANOGO_STAKING, 1)``).

    Raises ValueError on non-positive supply.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = _fields(cbor2.loads(raw))
    total, circulating = int(fields[0]), int(fields[1])
    if total <= 0 or circulating <= 0:
        raise ValueError("non-positive staking supply")
    return (total, circulating)


def parse_indigo(datum: str | bytes) -> tuple[int, int]:
    """Indigo oracle leaf -> iAsset price in ADA = price_int / 1_000_000.

    Datum is ``Constr0[Constr0[price_int], timestamp]``; Indigo publishes the price
    as ADA-per-iAsset scaled by 1e6. Returns (num, denom).

    The 1e6 scale is confirmed against on-chain usage: e.g. price_int 4330793 ->
    4.330793 ADA per iUSD, agreeing with the independent 1/(ADA->USD) estimate within
    the aggregator's deviation band (Indigo only ever appears as an intermediate hop,
    never a standalone collateral price).

    Raises ValueError on a non-positive price.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    inner = _fields(_fields(cbor2.loads(raw))[0])
    price = int(inner[0])
    if price <= 0:
        raise ValueError("non-positive indigo price")
    return (price, _INDIGO_SCALE)


def parse_liqwid_oracle_v2(datum: str | bytes) -> tuple[str, tuple[int, int]]:
    """Liqwid Oracle V2 leaf -> (priced_asset_unit, (num, denom)).

    Datum is a 6-field record ``[feed_id, Constr1[[asset]], [num, denom], ts, validity,
    config]``; the rate is the priced asset in the configured quote. Verified: ADA
    (the empty TupleAsset) at (16271, 100000) on tx 929a7d68...de8237 leaf
    ``(REF, TLIQWID_ORACLE_V2, 0)``.

    Raises ValueError on a non-positive denominator.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = _fields(cbor2.loads(raw))
    asset_unit_str = _tuple_asset_unit(_fields(fields[_LIQWID_ASSET_FIELD])[0])
    price = _fields(fields[_LIQWID_PRICE_FIELD])
    num, denom = int(price[0]), int(price[1])
    if denom <= 0:
        raise ValueError("non-positive liqwid oracle denom")
    return asset_unit_str, (num, denom)


def liqwid_oracle_feed_id(datum: str | bytes) -> str:
    """Liqwid Oracle V2 feed identifier (datum field 0) as hex.

    Several Liqwid feeds can price the same asset (e.g. two ADA feeds) and share one
    marker token at a common address, so the asset alone does not pin a UTxO; the
    field-0 feed id does and is stable across price updates. Empty string if absent.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    feed = _fields(cbor2.loads(raw))[0]
    if isinstance(feed, (bytes, bytearray)):
        return feed.hex()
    return str(feed)


def parse_orcfax_fs(datum: str | bytes) -> tuple[str, tuple[int, int]]:
    """Orcfax Feed Status leaf -> (feed_name, (num, denom)).

    Datum is ``Constr0[Constr0[feed_name, timestamp_ms, Constr0[num, denom]], owner]``;
    ``feed_name`` is the ASCII feed label (e.g. ``b"CER/ADA-USDM/3"``) and the inner
    ``Constr0[num, denom]`` is the published exchange rate. The companion Feed Status
    Pointer leaf carries only a bare bytestring pointing at this leaf's NFT policy and
    holds no rate, so the rate is read here.

    Raises ValueError on a non-positive denominator.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    body = _fields(_fields(cbor2.loads(raw))[0])
    price = _fields(body[2])
    num, denom = int(price[0]), int(price[1])
    if denom <= 0:
        raise ValueError("non-positive orcfax denom")
    feed = body[0]
    label = feed.decode() if isinstance(feed, (bytes, bytearray)) else str(feed)
    return label, (num, denom)


def parse_djed(datum: str | bytes) -> tuple[str, tuple[int, int]]:
    """DJED oracle leaf -> (pair_label, (num, denom)).

    Datum is ``Constr0[sig, Constr0[Constr0[num, denom], validity, pair], owner]``.
    Verified shape on tx 929a7d68...de8237 leaf ``(REF, TDJED, 12)``: pair ``b"USD"``,
    price pair (250000, 40719).

    Raises ValueError on a non-positive denominator.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    inner = _fields(_fields(cbor2.loads(raw))[1])
    price = _fields(inner[0])
    num, denom = int(price[0]), int(price[1])
    pair = inner[2]
    label = pair.decode() if isinstance(pair, bytes) else str(pair)
    if denom <= 0:
        raise ValueError("non-positive djed denom")
    return label, (num, denom)


def parse_minswap_lp(datum: str | bytes) -> tuple[str, int, str, int]:
    """Minswap V2 pool leaf -> (asset_a_unit, reserve_a, asset_b_unit, reserve_b).

    Datum field order is ``[stake, asset_a, asset_b, total_liquidity, reserve_a,
    reserve_b, ...]``. The caller derives a spot price via
    ``cpamm_price_from_reserves`` once it knows which side is token vs quote (set by
    the path's hop orientation). Verified on tx 929a7d68...de8237 leaf
    ``(REF, TMINSWAP_LP, 3)``: STRIKE/ADA reserves (1894509999077, 307838333882)
    feed the STRIKE->ADA hop of a 3-hop derived price.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = _fields(cbor2.loads(raw))
    return (
        _tuple_asset_unit(fields[_MS_ASSET_A_FIELD]),
        int(fields[_MS_RESERVE_A_FIELD]),
        _tuple_asset_unit(fields[_MS_ASSET_B_FIELD]),
        int(fields[_MS_RESERVE_B_FIELD]),
    )


def parse_splash_cpamm_g3(datum: str | bytes) -> tuple[str, str, str]:
    """Splash CPAMM (G3) pool datum -> (asset_x_unit, asset_y_unit, lp_unit).

    The reserves and LP balance are read from the pool UTxO *value* (not the datum);
    this just surfaces the three asset identities so the caller can pick the
    quote-side reserve and the LP balance. See ``splash_lp_token_price``.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = _fields(cbor2.loads(raw))
    return (
        _tuple_asset_unit(fields[_SPLASH_ASSET_X_FIELD]),
        _tuple_asset_unit(fields[_SPLASH_ASSET_Y_FIELD]),
        _tuple_asset_unit(fields[_SPLASH_LP_FIELD]),
    )


def splash_lp_token_price(
    *,
    reserve_quote: int,
    lp_balance: int,
    max_lp: int = _SPLASH_MAX_LP,
) -> tuple[int, int]:
    """Splash LP-token price in a pool asset = 2 * reserve_quote / circulating_lp.

    ``reserve_quote`` is the pool's balance of the quote asset (one of the pool's two
    assets), ``lp_balance`` is the LP tokens the pool still holds, so
    ``circulating_lp = max_lp - lp_balance``. The doubling is the single-side robust
    LP estimate (the other side converts to exactly ``reserve_quote`` at the pool's
    own spot price). Returns (num, denom).

    Verified bit-exact across every on-chain Splash LP-token pricing (29/29): e.g.
    reserve_quote=6193503, lp_balance=9223372036854735307 -> 12387006/40500 =
    76463/250 (tx dd724653...d2f4d37).

    Raises ValueError on non-positive circulating supply or reserve.
    """
    circulating_lp = max_lp - lp_balance
    if circulating_lp <= 0 or reserve_quote <= 0:
        raise ValueError("non-positive circulating LP or reserve")
    return (2 * reserve_quote, circulating_lp)


def parse_liqwid_market_state(datum: str | bytes) -> tuple[int, int]:
    """Liqwid market-state leaf -> qToken<->underlying exchange rate (state field 9).

    The 11-field state record carries the exchange rate as the rational at index 9
    (underlying per qToken). Returns (num, denom).

    Verified for the one Liqwid market that appears on-chain: with the rate
    360073866973321/12033383669199582 the qToken collateral price reproduces exactly
    as ``rate / 100 / ada_usd`` (tx 578fae97...), where the ``/100`` is the
    underlying's price and ``ada_usd`` the Liqwid ADA oracle. The two extra factors
    are separate path hops, not part of this leaf. All 8 on-chain occurrences are the
    same market, so the index-9 location is confirmed but not yet cross-checked
    against a second market.

    Raises ValueError on a non-positive rate.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    rate = _fields(cbor2.loads(raw))[_LIQWID_MARKET_RATE_FIELD]
    num, denom = int(rate[0]), int(rate[1])
    if num <= 0 or denom <= 0:
        raise ValueError("non-positive liqwid market exchange rate")
    return (num, denom)


def parse_liqwid_market_param(
    datum: str | bytes,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Liqwid market-parameter (config) leaf -> (base_rate, collateral_ratio).

    This leaf is NOT a price source: it carries the market's interest-rate model and
    risk parameters (the Danogo oracle takes the qToken exchange rate from the
    market-*state* leaf, never from here). Only the two unambiguously-typed rationals
    are surfaced -- the per-block base rate at field 0 and the collateral ratio at
    field 12 -- both as (num, denom); the remaining 30+ fields are left raw. Decoded
    for completeness so the source type is no longer an unparsed gap, and for future
    borrow-APY / health-factor work.

    On-chain config (tx daf74541...): base_rate=1/50, collateral_ratio=8696/10000.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = _fields(cbor2.loads(raw))
    base = fields[_LIQWID_PARAM_BASE_RATE_FIELD]
    coll = fields[_LIQWID_PARAM_COLLATERAL_RATIO_FIELD]
    return ((int(base[0]), int(base[1])), (int(coll[0]), int(coll[1])))


def liqwid_qtoken_rate(
    *,
    qtoken_rate_num: int,
    qtoken_rate_denom: int,
) -> tuple[int, int]:
    """QToken -> underlying rate as (num, denom). Raises on non-positive denom/num."""
    if qtoken_rate_num <= 0 or qtoken_rate_denom <= 0:
        raise ValueError("non-positive qtoken rate")
    return (qtoken_rate_num, qtoken_rate_denom)
