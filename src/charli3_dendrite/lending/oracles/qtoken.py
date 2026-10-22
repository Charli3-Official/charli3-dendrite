"""Receipt-token (qToken/dToken) exchange-rate pricing.

`QTokenResolver` turns a rate supplied per-call via OracleRef.extra (`num`/`denom`),
sourced from the pool/market datum by the concrete protocol class, into a price.

`receipt_price` prices one receipt token in lovelace: a receipt token is worth its
market's exchange rate in the market's borrow unit, so its lovelace price is that rate
times the borrow unit's lovelace price. `receipt_prices` applies it to the receipt
tokens of pooled markets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap

if TYPE_CHECKING:
    from collections.abc import Iterable

    from charli3_dendrite.lending.normalized import LendingMarket


class QTokenResolver:
    """Normalizes a supplied qToken exchange rate to an OraclePrice."""

    source = OracleSource.QTOKEN_RATE

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """UTxO selectors (none: rate is supplied in-band by the pool state)."""
        return []

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Build an OraclePrice from the rate supplied in `ref.extra`."""
        num = ref.extra.get("num")
        denom = ref.extra.get("denom")
        if num is None or denom is None:
            return None
        # A non-numeric `extra` value should yield no price, not an exception,
        # mirroring the sibling resolvers' defensive parsing.
        try:
            num = int(num)
            denom = int(denom)
        except (TypeError, ValueError):
            return None
        # Refuse a non-positive rate that would later zero-divide or invert in
        # `OraclePrice.as_decimal()`, mirroring the sibling resolvers' guard.
        if denom <= 0 or num <= 0:
            return None
        return OraclePrice(
            token=ref.token,
            quote=ref.quote,
            num=num,
            denom=denom,
            source=self.source,
            valid_from=ref.extra.get("valid_from"),
            valid_to=ref.extra.get("valid_to"),
        )


def _lovelace_price(prices: PriceMap, unit: str) -> tuple[int, int] | None:
    """``unit``'s positive lovelace price as ``(num, denom)``, or None when absent.

    Lovelace is worth one lovelace whether or not ``prices`` holds an entry for it.
    """
    if unit == "lovelace":
        return 1, 1
    price = prices.get(unit)
    if price is None or price.num <= 0 or price.denom <= 0:
        return None
    return price.num, price.denom


def receipt_price(
    token: str,
    rate: tuple[int, int],
    underlying_unit: str,
    prices: PriceMap,
    *,
    source: OracleSource = OracleSource.QTOKEN_RATE,
    valid_from: int | None = None,
) -> OraclePrice | None:
    """Lovelace price of receipt token ``token``: ``rate`` times its underlying's.

    ``rate`` is the exchange rate as ``(num, denom)``, underlying base units per
    receipt base unit; the price is the exact, unreduced product of that rational and
    ``underlying_unit``'s lovelace price in ``prices`` (lovelace is worth one
    lovelace without an entry). None when either part is not positive or the
    underlying has no price.

    Args:
        token: The receipt-token unit to price.
        rate: The exchange rate as ``(num, denom)``.
        underlying_unit: The unit the receipt token redeems for.
        prices: Lovelace prices of underlying units. It is not modified.
        source: The source tag of the returned price.
        valid_from: The start of the returned price's validity, if known.
    """
    rate_num, rate_denom = rate
    if rate_num <= 0 or rate_denom <= 0:
        return None
    underlying = _lovelace_price(prices, underlying_unit)
    if underlying is None:
        return None
    num, denom = underlying
    return OraclePrice(
        token=token,
        quote="lovelace",
        num=rate_num * num,
        denom=rate_denom * denom,
        source=source,
        valid_from=valid_from,
    )


def receipt_prices(markets: Iterable[LendingMarket], prices: PriceMap) -> PriceMap:
    """Lovelace prices for the receipt tokens of ``markets``.

    Each market with a ``receipt_unit`` and a positive ``receipt_rate`` gets the price
    ``receipt_rate`` times its borrow unit's lovelace price (:func:`receipt_price`).
    Markets whose borrow unit has no positive lovelace price in ``prices`` are left
    out.

    Args:
        markets: Market views; those without a receipt token are skipped.
        prices: Lovelace prices of the borrow units. It is not modified.

    Returns:
        A new map holding only the receipt-token prices, to be merged into a price
        book with :meth:`PriceMap.add`.
    """
    receipts = PriceMap()
    for market in markets:
        rate = market.receipt_rate
        if market.receipt_unit is None or rate is None:
            continue
        price = receipt_price(
            market.receipt_unit,
            (rate.numerator, rate.denominator),
            market.borrow_unit,
            prices,
        )
        if price is not None:
            receipts.add(price)
    return receipts
