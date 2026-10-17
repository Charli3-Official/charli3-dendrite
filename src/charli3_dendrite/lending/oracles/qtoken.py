"""Receipt-token (qToken/dToken) exchange-rate pricing.

`QTokenResolver` turns a rate supplied per-call via OracleRef.extra (`num`/`denom`),
sourced from the pool/market datum by the concrete protocol class, into a price.

`receipt_prices` prices the receipt tokens of pooled markets in lovelace: a receipt
token is worth its market's exchange rate in the market's borrow unit, so its lovelace
price is that rate times the borrow unit's lovelace price.
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


def receipt_prices(markets: Iterable[LendingMarket], prices: PriceMap) -> PriceMap:
    """Lovelace prices for the receipt tokens of ``markets``.

    Each market with a ``receipt_unit`` and a positive ``receipt_rate`` gets the price
    ``receipt_rate`` times its borrow unit's lovelace price, as the exact product of
    the two rationals. Markets whose borrow unit has no positive lovelace price in
    ``prices`` are left out.

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
        if market.receipt_unit is None or rate is None or rate <= 0:
            continue
        underlying = _lovelace_price(prices, market.borrow_unit)
        if underlying is None:
            continue
        num, denom = underlying
        receipts.add(
            OraclePrice(
                token=market.receipt_unit,
                quote="lovelace",
                num=rate.numerator * num,
                denom=rate.denominator * denom,
                source=OracleSource.QTOKEN_RATE,
            ),
        )
    return receipts
