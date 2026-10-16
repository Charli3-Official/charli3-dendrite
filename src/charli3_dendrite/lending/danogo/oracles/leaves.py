"""Leaf-source price extractors for the Danogo oracle aggregator.

This module holds the pure per-leaf price arithmetic the aggregation engine depends
on. Each function maps a leaf oracle source's confirmed datum fields to a
``(num, denom)`` rate, matching the on-chain ``handle_oracle_idx`` per-type logic
documented in Danogo's oracle-aggregator audit.

Verified bit-exact against on-chain ``OraclePriceCalcRdmr`` prices (tx
929a7d68...de8237 and 590d...e6a069):
* ``danogo_pool_dtoken_rate`` (``TDANOGO_POOL``).
* ``parse_danogo_staking_rate`` (``TDANOGO_STAKING``).
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

The Liqwid leaves (``TLIQWID_ORACLE_V2``, ``TLIQWID_MARKET_STATE`` and
``TLIQWID_MARKET_PARAM``) are parsed by ``charli3_dendrite.lending.liqwid.datums``.

Every ``OracleUtxoType`` observed in full mainnet history (9 of the 17 registry
entries) now has a parser, including ``TLIQWID_MARKET_PARAM`` (a config record, not a
price). Not implemented (never seen on-chain): ``TORCFAX_FSP/FS``,
``TLIQWID_ORACLE_V1``, ``TSPLASH_CPAMM_G1/G2``, ``TSPLASH_STABLE``, ``TCHARLI3``,
``TMINSWAP_LP_STABLE``.
"""

from __future__ import annotations

import cbor2  # type: ignore[import-not-found]

# Kept for compatibility: the Liqwid parsers live in
# ``charli3_dendrite.lending.liqwid.datums``. These re-exports will be removed in a
# later minor release.
from charli3_dendrite.lending.liqwid.datums import liqwid_oracle_feed_id  # noqa: F401
from charli3_dendrite.lending.liqwid.datums import liqwid_qtoken_rate  # noqa: F401
from charli3_dendrite.lending.liqwid.datums import (  # noqa: F401
    parse_liqwid_market_param,
)
from charli3_dendrite.lending.liqwid.datums import (  # noqa: F401
    parse_liqwid_market_state,
)
from charli3_dendrite.lending.liqwid.datums import parse_liqwid_oracle_v2  # noqa: F401
from charli3_dendrite.lending.units import plutus_fields
from charli3_dendrite.lending.units import tuple_asset_unit

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
    fields = plutus_fields(cbor2.loads(raw))
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
    inner = plutus_fields(plutus_fields(cbor2.loads(raw))[0])
    price = int(inner[0])
    if price <= 0:
        raise ValueError("non-positive indigo price")
    return (price, _INDIGO_SCALE)


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
    body = plutus_fields(plutus_fields(cbor2.loads(raw))[0])
    price = plutus_fields(body[2])
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
    inner = plutus_fields(plutus_fields(cbor2.loads(raw))[1])
    price = plutus_fields(inner[0])
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
    fields = plutus_fields(cbor2.loads(raw))
    return (
        tuple_asset_unit(fields[_MS_ASSET_A_FIELD]),
        int(fields[_MS_RESERVE_A_FIELD]),
        tuple_asset_unit(fields[_MS_ASSET_B_FIELD]),
        int(fields[_MS_RESERVE_B_FIELD]),
    )


def parse_splash_cpamm_g3(datum: str | bytes) -> tuple[str, str, str]:
    """Splash CPAMM (G3) pool datum -> (asset_x_unit, asset_y_unit, lp_unit).

    The reserves and LP balance are read from the pool UTxO *value* (not the datum);
    this just surfaces the three asset identities so the caller can pick the
    quote-side reserve and the LP balance. See ``splash_lp_token_price``.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = plutus_fields(cbor2.loads(raw))
    return (
        tuple_asset_unit(fields[_SPLASH_ASSET_X_FIELD]),
        tuple_asset_unit(fields[_SPLASH_ASSET_Y_FIELD]),
        tuple_asset_unit(fields[_SPLASH_LP_FIELD]),
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
