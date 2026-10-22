"""The shared Liqwid fixture helpers: parsed records, live feeds and price maps."""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.market import market_to_view
from charli3_dendrite.lending.liqwid.oracles import liqwid_price_map
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.qtoken import receipt_prices
from tests.lending.liqwid import records
from tests.lending.liqwid import test_market
from tests.lending.liqwid import test_math
from tests.lending.liqwid.records import UTXO_KINDS
from tests.lending.liqwid.records import live_context
from tests.lending.liqwid.records import live_feeds
from tests.lending.liqwid.records import live_price_map
from tests.lending.liqwid.records import parsed_records
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of

DEPLOYMENT = default_deployment()


@pytest.fixture(scope="module")
def live_markets():
    context = live_context()
    return [
        market_to_view(record, context, DEPLOYMENT)
        for _, record in parsed_records("market_state", live=True)
    ]


@pytest.mark.parametrize("live", [None, True, False])
@pytest.mark.parametrize("kind", UTXO_KINDS)
def test_parsed_records_keep_every_entry_that_parses(kind, live):
    expected = []
    for rec in records_of(kind, live=live):
        record = parse_utxo(record_info(rec), DEPLOYMENT)
        if record is not None:
            expected.append((rec, record))
    assert parsed_records(kind, live=live) == expected
    if live is None:
        assert expected


def test_live_feeds_are_every_live_oracle_output_that_parses():
    expected = [
        feed
        for feed in (
            parse_feed(record_info(rec), DEPLOYMENT)
            for rec in records_of("oracle", live=True)
        )
        if feed is not None
    ]
    assert expected
    assert live_feeds() == expected


def test_live_price_map_is_the_liqwid_price_map_of_the_live_feeds():
    assert live_price_map() == liqwid_price_map(live_feeds())
    assert live_price_map(()) == live_price_map()


def test_live_price_map_adds_the_receipt_prices_of_the_markets(live_markets):
    prices = liqwid_price_map(live_feeds())
    receipts = receipt_prices(live_markets, prices).prices
    merged = PriceMap(prices={**prices.prices, **receipts})
    added = liqwid_price_map(live_feeds())
    for price in receipts.values():
        added.add(price)
    book = live_price_map(live_markets)
    assert receipts
    assert book == merged == added
    assert set(book.prices) == set(prices.prices) | set(receipts)


@pytest.mark.parametrize("module", [test_math, test_market])
def test_math_and_market_tests_use_the_shared_helpers(module):
    assert module.parsed_records is records.parsed_records
    assert module.live_price_map is records.live_price_map
    assert not hasattr(module, "_parsed")
