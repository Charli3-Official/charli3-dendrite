"""Liqwid Finance datum parsers.

Pure decoders for the Liqwid on-chain records that are read as price and rate
sources: the Oracle V2 price feed, the market state (qToken exchange rate), and the
market parameters (risk configuration). Each parser takes the datum as CBOR hex or
raw bytes.
"""

from __future__ import annotations

import cbor2  # type: ignore[import-not-found]

from charli3_dendrite.lending.units import plutus_fields
from charli3_dendrite.lending.units import tuple_asset_unit

# Liqwid Oracle V2 datum field order: [updater_key, Constr1[[asset]], [num, denom],
# timestamp_ms, validity_ms, config].
_LIQWID_ASSET_FIELD = 1
_LIQWID_PRICE_FIELD = 2
# Liqwid market-state datum: the qToken<->underlying exchange rate is the rational at
# index 9 of the 11-field state record.
_LIQWID_MARKET_RATE_FIELD = 9
# Liqwid market-parameter (config) datum: the initial qToken exchange rate at field 0
# and the maximum loan-to-value on weighted collateral at field 12.
_LIQWID_PARAM_INITIAL_QTOKEN_RATE_FIELD = 0
_LIQWID_PARAM_MAX_LTV_FIELD = 12


def parse_liqwid_oracle_v2(datum: str | bytes) -> tuple[str, tuple[int, int]]:
    """Liqwid Oracle V2 datum -> (priced_asset_unit, (num, denom)).

    Datum is a 6-field record ``[updater_key, Constr1[[asset]], [num, denom], ts,
    validity, config]``; the rate is the priced asset (field 1) in the feed's
    configured quote. ADA (the empty TupleAsset) is ``"lovelace"``.

    Raises ValueError on a non-positive denominator.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = plutus_fields(cbor2.loads(raw))
    asset_unit_str = tuple_asset_unit(plutus_fields(fields[_LIQWID_ASSET_FIELD])[0])
    price = plutus_fields(fields[_LIQWID_PRICE_FIELD])
    num, denom = int(price[0]), int(price[1])
    if denom <= 0:
        raise ValueError("non-positive liqwid oracle denom")
    return asset_unit_str, (num, denom)


def liqwid_oracle_feed_id(datum: str | bytes) -> str:
    """Liqwid Oracle V2 datum field 0 -- the oracle updater's key -- as hex.

    The updater key is shared by every feed that updater publishes, so it does not
    identify a feed on its own. Nor does the oracle token plus the priced asset
    (field 1, see ``parse_liqwid_oracle_v2``): one oracle token can carry more than
    one feed for the same asset. The updater key together with the priced asset
    distinguishes the feeds that share an oracle token. A field 0 that is not a
    bytestring is returned as its ``str``.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    feed = plutus_fields(cbor2.loads(raw))[0]
    if isinstance(feed, (bytes, bytearray)):
        return feed.hex()
    return str(feed)


def parse_liqwid_market_state(datum: str | bytes) -> tuple[int, int]:
    """Liqwid market-state datum -> qToken exchange rate (state field 9).

    Field 9 of the 11-field state record is the qToken exchange rate (underlying per
    qToken), a rational. Returns (num, denom).

    Raises ValueError on a non-positive rate.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    rate = plutus_fields(cbor2.loads(raw))[_LIQWID_MARKET_RATE_FIELD]
    num, denom = int(rate[0]), int(rate[1])
    if num <= 0 or denom <= 0:
        raise ValueError("non-positive liqwid market exchange rate")
    return (num, denom)


def parse_liqwid_market_param(
    datum: str | bytes,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Liqwid market-parameter (config) datum -> (initial_qtoken_rate, max_ltv).

    This datum is the market's interest-model and risk configuration, not a price
    source (the live qToken exchange rate is read from the market-state datum, see
    ``parse_liqwid_market_state``). Two rationals are surfaced, both as (num, denom):

    * field 0, the initial qToken exchange rate (underlying per qToken) a new market
      starts from, e.g. 1/50;
    * field 12, the maximum loan-to-value on weighted collateral; the protocol's
      minimum health factor is its reciprocal.

    The remaining fields are left raw.
    """
    raw = bytes.fromhex(datum) if isinstance(datum, str) else datum
    fields = plutus_fields(cbor2.loads(raw))
    initial = fields[_LIQWID_PARAM_INITIAL_QTOKEN_RATE_FIELD]
    max_ltv = fields[_LIQWID_PARAM_MAX_LTV_FIELD]
    return ((int(initial[0]), int(initial[1])), (int(max_ltv[0]), int(max_ltv[1])))


def liqwid_qtoken_rate(
    *,
    qtoken_rate_num: int,
    qtoken_rate_denom: int,
) -> tuple[int, int]:
    """QToken -> underlying rate as (num, denom). Raises on non-positive denom/num."""
    if qtoken_rate_num <= 0 or qtoken_rate_denom <= 0:
        raise ValueError("non-positive qtoken rate")
    return (qtoken_rate_num, qtoken_rate_denom)
