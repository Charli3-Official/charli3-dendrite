from decimal import Decimal
from fractions import Fraction
from math import ceil

from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.normalized import _price_in
from charli3_dendrite.lending.oracles.models import CORE_ORACLE_SOURCES
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.qtoken import QTokenResolver
from charli3_dendrite.lending.oracles.qtoken import receipt_price
from charli3_dendrite.lending.oracles.qtoken import receipt_prices
from tests.lending.views import MIN
from tests.lending.views import SNEK
from tests.lending.views import market
from tests.lending.views import prices
from tests.lending.views import terms

# Receipt-token units (policy-only, as a pooled market mints them) and a second borrow
# unit to cross into.
QADA = "c1" * 28
QSNEK = "c2" * 28
QMIN = "c3" * 28
DJED = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344"
)
# An exchange rate as a pooled market stores it: underlying units per receipt unit.
S9 = Fraction(20_457_318_331, 1_000_000_000)


def _merged(book: PriceMap, extra: PriceMap) -> PriceMap:
    """A copy of ``book`` with every price of ``extra`` added through ``PriceMap.add``."""
    out = PriceMap()
    for price in [*book.prices.values(), *extra.prices.values()]:
        out.add(price)
    return out


def test_qtoken_rate_from_extra():
    ref = OracleRef(
        source=OracleSource.QTOKEN_RATE,
        token="qADA",
        quote="lovelace",
        extra={"num": 1_050_000, "denom": 1_000_000, "valid_from": 1, "valid_to": 9},
    )
    price = QTokenResolver().resolve(ref, PoolStateList(root=[]))
    assert price.as_decimal() == Decimal("1.05")
    assert price.valid_from == 1
    assert price.valid_to == 9
    assert price.source is OracleSource.QTOKEN_RATE


def test_qtoken_missing_rate_returns_none():
    ref = OracleRef(source=OracleSource.QTOKEN_RATE, token="qADA")
    assert QTokenResolver().resolve(ref, PoolStateList(root=[])) is None


def test_qtoken_non_positive_rate_returns_none():
    for extra in (
        {"num": 1_050_000, "denom": 0},
        {"num": 1_050_000, "denom": -1},
        {"num": 0, "denom": 1_000_000},
        {"num": -5, "denom": 1_000_000},
    ):
        ref = OracleRef(source=OracleSource.QTOKEN_RATE, token="qADA", extra=extra)
        assert QTokenResolver().resolve(ref, PoolStateList(root=[])) is None


def test_qtoken_non_numeric_rate_returns_none():
    ref = OracleRef(
        source=OracleSource.QTOKEN_RATE,
        token="qADA",
        extra={"num": "abc", "denom": 1_000_000},
    )
    assert QTokenResolver().resolve(ref, PoolStateList(root=[])) is None


def test_liqwid_is_a_shared_source_outside_the_core_set():
    assert OracleSource("liqwid") is OracleSource.LIQWID
    assert OracleSource.LIQWID.value == "liqwid"
    assert OracleSource.LIQWID not in CORE_ORACLE_SOURCES


def test_a_liqwid_oracle_ref_round_trips_through_its_dump():
    # Collateral terms persist their price source with `model_dump` and read it
    # back with `model_validate`, so the member must exist on the shared enum.
    ref = OracleRef(
        source=OracleSource.LIQWID,
        token=QSNEK,
        extra={"market_state": "ab" * 28},
    )
    back = OracleRef.model_validate(ref.model_dump())
    assert back == ref
    assert back.source is OracleSource.LIQWID


def test_receipt_prices_scale_the_underlying_lovelace_price():
    book = prices((SNEK, "lovelace", 3_217, 1_000_000))
    qsnek = market("qsnek", borrow_unit=SNEK, receipt_unit=QSNEK, receipt_rate=S9)

    receipts = receipt_prices([qsnek], book)

    price = receipts.get(QSNEK)
    assert price is not None
    assert price.token == QSNEK
    assert price.quote == "lovelace"
    assert price.source is OracleSource.QTOKEN_RATE
    assert price.num == S9.numerator * 3_217
    assert price.denom == S9.denominator * 1_000_000
    assert Fraction(price.num, price.denom) == S9 * Fraction(3_217, 1_000_000)
    # Only the receipt entry is returned, and the input map is left alone.
    assert list(receipts.prices) == [(QSNEK, "lovelace")]
    assert list(book.prices) == [(SNEK, "lovelace")]


def test_receipt_prices_skip_markets_without_a_receipt_unit_or_rate():
    book = prices((SNEK, "lovelace", 3_217, 1_000_000))
    no_unit = market("no-unit", borrow_unit=SNEK, receipt_unit=None, receipt_rate=S9)
    no_rate = market("no-rate", borrow_unit=SNEK, receipt_unit=QSNEK)
    zero_rate = market(
        "zero-rate",
        borrow_unit=SNEK,
        receipt_unit=QSNEK,
        receipt_rate=Fraction(0),
    )
    assert receipt_prices([no_unit, no_rate, zero_rate], book).prices == {}


def test_an_ada_market_prices_its_receipt_at_exactly_its_rate():
    # No `lovelace` entry: lovelace is worth one lovelace without one.
    qada = market("qada", borrow_unit="lovelace", receipt_unit=QADA, receipt_rate=S9)
    for book in (PriceMap(), prices((SNEK, "lovelace", 3_217, 1_000_000))):
        price = receipt_prices([qada], book).get(QADA)
        assert price is not None
        assert price.num == S9.numerator
        assert price.denom == S9.denominator
        assert Fraction(price.num, price.denom) == S9


def test_receipt_prices_omit_unpriced_and_zero_priced_underlyings():
    book = prices((SNEK, "lovelace", 0, 1_000_000))
    unpriced = market("qmin", borrow_unit=MIN, receipt_unit=QMIN, receipt_rate=S9)
    zero = market("qsnek", borrow_unit=SNEK, receipt_unit=QSNEK, receipt_rate=S9)
    qada = market("qada", borrow_unit="lovelace", receipt_unit=QADA, receipt_rate=S9)

    receipts = receipt_prices([unpriced, zero, qada], book)

    assert receipts.get(QMIN) is None
    assert receipts.get(QSNEK) is None
    assert list(receipts.prices) == [(QADA, "lovelace")]


def test_a_receipt_price_crosses_into_another_borrow_unit_through_lovelace():
    book = prices(
        (SNEK, "lovelace", 3_217, 1_000_000),
        (DJED, "lovelace", 2_850_000, 1_000_000),
    )
    qsnek = market("qsnek", borrow_unit=SNEK, receipt_unit=QSNEK, receipt_rate=S9)
    merged = _merged(book, receipt_prices([qsnek], book))

    expected = S9 * Fraction(3_217, 1_000_000) / Fraction(2_850_000, 1_000_000)
    assert _price_in(merged, QSNEK, DJED) == expected

    djed_market = market("djed", borrow_unit=DJED, collateral=(terms(QSNEK),))
    # Without the hop the receipt token has no price in the borrow unit.
    assert djed_market.required_collateral(QSNEK, 100_000_000, book) is None
    assert djed_market.required_collateral(QSNEK, 100_000_000, merged) == ceil(
        Fraction(100_000_000) / (Fraction(2, 3) * expected),
    )


def test_receipt_price_is_the_exact_unreduced_product_of_rate_and_underlying():
    book = prices((SNEK, "lovelace", 3_217, 1_000_000))
    price = receipt_price(QSNEK, (40, 20), SNEK, book)
    assert price is not None
    assert (price.token, price.quote) == (QSNEK, "lovelace")
    assert (price.num, price.denom) == (40 * 3_217, 20 * 1_000_000)
    assert price.source is OracleSource.QTOKEN_RATE
    assert (price.valid_from, price.valid_to) == (None, None)
    stamped = receipt_price(
        QSNEK,
        (40, 20),
        SNEK,
        book,
        source=OracleSource.LIQWID,
        valid_from=1_234,
    )
    assert stamped is not None
    assert stamped.source is OracleSource.LIQWID
    assert stamped.valid_from == 1_234
    assert (stamped.num, stamped.denom) == (price.num, price.denom)


def test_receipt_price_prices_lovelace_at_one_without_an_entry():
    price = receipt_price(QADA, (7, 3), "lovelace", PriceMap())
    assert price is not None
    assert (price.num, price.denom) == (7, 3)


def test_receipt_price_refuses_a_non_positive_rate_or_underlying():
    book = prices((SNEK, "lovelace", 3_217, 1_000_000), (MIN, "lovelace", 0, 1))
    for rate in ((0, 1), (1, 0), (-1, 2), (1, -2), (-1, -2)):
        assert receipt_price(QSNEK, rate, SNEK, book) is None
    assert receipt_price(QMIN, (1, 1), MIN, book) is None
    assert receipt_price(QADA, (1, 1), DJED, book) is None


def test_receipt_prices_price_every_market_through_receipt_price(monkeypatch):
    import charli3_dendrite.lending.oracles.qtoken as qtoken

    calls = []
    real = qtoken.receipt_price

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(qtoken, "receipt_price", spy)
    book = prices((SNEK, "lovelace", 3_217, 1_000_000))
    qsnek = market("qsnek", borrow_unit=SNEK, receipt_unit=QSNEK, receipt_rate=S9)
    receipts = qtoken.receipt_prices([qsnek], book)
    assert calls == [(QSNEK, (S9.numerator, S9.denominator), SNEK, book)]
    assert receipts.get(QSNEK) == real(
        QSNEK, (S9.numerator, S9.denominator), SNEK, book
    )
