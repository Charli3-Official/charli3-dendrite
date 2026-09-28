"""Market-source registry and the market book (offline)."""

import pytest

from charli3_dendrite.lending import markets
from charli3_dendrite.lending.fluidtokens_v4.market import FluidTokensV4MarketSource
from charli3_dendrite.lending.markets import LendingMarketBook
from charli3_dendrite.lending.markets import LendingSnapshot
from charli3_dendrite.lending.markets import available_market_sources
from charli3_dendrite.lending.markets import get_market_source
from charli3_dendrite.lending.markets import load_all
from charli3_dendrite.lending.markets import register_market_source
from charli3_dendrite.lending.rates import MS_PER_HOUR
from tests.lending.fluidtokens_v4.records import FIX
from tests.lending.fluidtokens_v4.test_loader import NOW_MS
from tests.lending.fluidtokens_v4.test_loader import FakeBackend
from tests.lending.views import MIN
from tests.lending.views import SNEK
from tests.lending.views import SimpleRate
from tests.lending.views import market
from tests.lending.views import prices
from tests.lending.views import request
from tests.lending.views import terms

_YEAR_MS = 8760 * MS_PER_HOUR


class _Source:
    def __init__(self, name, snapshot):
        self.name = name
        self._snapshot = snapshot

    def selectors(self):
        return []

    def parse(self, info):
        return None

    def load(self, backend, now_ms=None):
        return self._snapshot


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(markets, "_SOURCES", {})
    monkeypatch.setattr(markets, "_BUILTINS_LOADED", True)
    return markets._SOURCES


def test_fluidtokens_v4_is_registered():
    assert "fluidtokensv4" in available_market_sources()
    assert isinstance(get_market_source("FluidTokensV4"), FluidTokensV4MarketSource)


def test_an_unknown_source_names_the_known_ones():
    with pytest.raises(KeyError, match="fluidtokensv4"):
        get_market_source("nope")


def test_load_all_merges_every_source_in_name_order(registry):
    register_market_source(_Source("b", LendingSnapshot(markets=(market("b1"),))))
    register_market_source(
        _Source(
            "A",
            LendingSnapshot(markets=(market("a1"),), requests=(request(),)),
        ),
    )
    merged = load_all(backend=None)
    assert [m.market_id for m in merged.markets] == ["a1", "b1"]
    assert merged.requests == (request(),)


def test_the_v4_source_loads_the_captured_protocol():
    snapshot = FluidTokensV4MarketSource().load(FakeBackend(), now_ms=NOW_MS)
    assert len(snapshot.markets) == len(FIX["pool"])
    assert len(snapshot.positions) == len(FIX["loan"])
    assert snapshot.requests == ()
    # Every captured pool has its manager, so every lender is the owner's key.
    assert {m.lender.kind.value for m in snapshot.markets} == {"key"}


def test_borrow_options_rank_by_cost_then_market_id():
    book = LendingMarketBook(
        [
            market("dear", rate_model=SimpleRate(900)),
            market("cheap-b", rate_model=SimpleRate(300)),
            market("cheap-a", rate_model=SimpleRate(300)),
        ],
    )
    options = book.borrow_options("lovelace", SNEK, 1_000_000_000, _YEAR_MS)
    assert [o.market.market_id for o in options] == ["cheap-a", "cheap-b", "dear"]
    assert [o.interest for o in options] == [30_000_000, 30_000_000, 90_000_000]
    assert all(o.required_collateral is None for o in options)  # no prices given


def test_borrow_options_leave_out_markets_that_cannot_serve_the_borrow():
    book = LendingMarketBook(
        [
            market("ok"),
            market("thin", available_liquidity=999),
            market("other-token", borrow_unit=MIN),
            market("no-snek", collateral=(terms(MIN),)),
            market("kyc", permissioned=True),
        ],
    )
    served = book.borrow_options("lovelace", SNEK, 1_000, 0)
    assert [o.market.market_id for o in served] == ["ok"]
    with_kyc = book.borrow_options(
        "lovelace", SNEK, 1_000, 0, include_permissioned=True
    )
    assert [o.market.market_id for o in with_kyc] == ["kyc", "ok"]


def test_borrow_options_price_the_collateral_when_they_can():
    book = LendingMarketBook([market()])
    (option,) = book.borrow_options(
        "lovelace",
        SNEK,
        1_000_000,
        0,
        prices((SNEK, "lovelace", 3, 1000)),
    )
    assert option.required_collateral == 500_000_000


def test_lend_options_rank_requests_by_headline_rate():
    book = LendingMarketBook(
        [],
        [
            request("low", rate_model=SimpleRate(100)),
            request("high", rate_model=SimpleRate(900)),
            request("other", borrow_unit=MIN, rate_model=SimpleRate(5000)),
        ],
    )
    assert [r.request_id for r in book.lend_options("lovelace")] == ["high", "low"]


def test_a_book_from_a_snapshot():
    snapshot = LendingSnapshot(markets=(market(),), requests=(request(),))
    book = LendingMarketBook.from_snapshot(snapshot)
    assert book.markets == (market(),)
    assert book.requests == (request(),)
