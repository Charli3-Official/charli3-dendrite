"""Market-source registry and the market book (offline)."""

import dataclasses
from fractions import Fraction

import pytest

from charli3_dendrite.lending import markets
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.market import FluidTokensV4MarketSource
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidFlatTermRate
from charli3_dendrite.lending.liqwid.lifecycle import LoanKey
from charli3_dendrite.lending.liqwid.lifecycle import LoanSpend
from charli3_dendrite.lending.liqwid.lifecycle import LoanTransition
from charli3_dendrite.lending.markets import CONTEXT_ROLE
from charli3_dendrite.lending.markets import VIEW_ROLE
from charli3_dendrite.lending.markets import LendingMarketBook
from charli3_dendrite.lending.markets import LendingSnapshot
from charli3_dendrite.lending.markets import ParseContext
from charli3_dendrite.lending.markets import PositionLifecycle
from charli3_dendrite.lending.markets import WatchSpec
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
_RECEIPT = "a3" * 28 + "71"
_LOAN_TOKEN_POLICY = "cc" * 28


class _Source:
    lifecycle = None

    def __init__(self, name, snapshot):
        self.name = name
        self._snapshot = snapshot

    def selectors(self):
        return []

    def parse(self, info, *, context=None):
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


def test_a_source_registered_before_the_first_lookup_is_kept(monkeypatch):
    monkeypatch.setattr(markets, "_SOURCES", {})
    monkeypatch.setattr(markets, "_BUILTINS_LOADED", False)
    mine = _Source("FluidTokensV4", LendingSnapshot())
    register_market_source(mine)
    assert get_market_source("fluidtokensv4") is mine


def test_borrow_options_leave_out_terms_shorter_than_the_borrow():
    month = FluidFlatTermRate(
        interest_rate=100,
        total_installments=1,
        installment_period=720,
        initial_grace_period=0,
        repayment_time_window=0,
        penalty_fee_for_late_repayment=0,
    )
    book = LendingMarketBook([market("month", rate_model=month), market("open")])
    term_ms = 720 * MS_PER_HOUR
    within = book.borrow_options("lovelace", SNEK, 1_000, term_ms)
    assert sorted(o.market.market_id for o in within) == ["month", "open"]
    beyond = book.borrow_options("lovelace", SNEK, 1_000, term_ms + 1)
    assert [o.market.market_id for o in beyond] == ["open"]


def test_lend_options_leave_out_expired_and_permissioned_requests():
    book = LendingMarketBook(
        [],
        [
            request("live"),
            request("expired", expires_ms=100),
            request("kyc", permissioned=True),
        ],
    )
    assert [r.request_id for r in book.lend_options("lovelace", now_ms=100)] == ["live"]
    with_kyc = book.lend_options("lovelace", now_ms=100, include_permissioned=True)
    assert [r.request_id for r in with_kyc] == ["kyc", "live"]
    assert [r.request_id for r in book.lend_options("lovelace")] == ["expired", "live"]


def test_a_watch_spec_watches_views_by_default():
    spec = WatchSpec("ab" * 28, None)
    assert spec.role == VIEW_ROLE == "view"
    assert CONTEXT_ROLE == "context"
    assert spec == WatchSpec("ab" * 28, None, role=VIEW_ROLE)
    assert hash(spec) == hash(WatchSpec("ab" * 28, None, VIEW_ROLE))
    assert spec != WatchSpec("ab" * 28, None, role=CONTEXT_ROLE)


def test_the_v4_selectors_keep_their_two_field_form():
    selectors = entity_selectors()
    expected = [
        WatchSpec(selectors[kind].payment_credential, selectors[kind].identity_policy)
        for kind in (EntityKind.POOL, EntityKind.LOAN, EntityKind.REQUEST)
    ]
    specs = FluidTokensV4MarketSource().selectors()
    assert specs == expected
    assert {spec.role for spec in specs} == {VIEW_ROLE}


def test_a_parse_context_defaults_to_no_utxos():
    context = ParseContext(slot=None)
    assert context.utxos == ()
    assert context == ParseContext(None, ())
    assert ParseContext(5).slot == 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.slot = 1


class _ChainedLifecycle:
    loan_token_policies = frozenset({_LOAN_TOKEN_POLICY})

    def loan_key(self, info):
        return None

    def match_successors(self, spent, created):
        return {}

    def classify_spend(self, spent, successor, spend):
        if successor is None:
            return LoanTransition.REPAID
        return LoanTransition.MODIFIED


def test_a_chained_lifecycle_satisfies_the_protocol():
    assert isinstance(_ChainedLifecycle(), PositionLifecycle)
    assert not isinstance(object(), PositionLifecycle)


def test_the_v4_source_has_no_chained_lifecycle():
    assert FluidTokensV4MarketSource().lifecycle is None
    assert FluidTokensV4MarketSource.lifecycle is None
    assert get_market_source("fluidtokensv4").lifecycle is None


def _loan_key(**overrides):
    values = {
        "out_ref": "aa" * 32 + "#0",
        "market_id": "bb" * 28 + "4d61726b6574",
        "owner": "dd" * 28,
        "principal": 100_000_000,
        "interest": 3_000,
        "min_interest": 80_000,
        "loan_index": 1_000_000_000_007,
        "collateral": ((_RECEIPT, 5_000_000_000),),
        "generation": "new",
    }
    values.update(overrides)
    return LoanKey(**values)


def test_loan_keys_are_frozen_hashable_values():
    key = _loan_key()
    assert key == _loan_key()
    assert len({key, _loan_key()}) == 1
    assert key != _loan_key(out_ref="aa" * 32 + "#1")
    assert _loan_key(owner=None).owner is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.principal = 0


def test_a_loan_spend_compares_its_mints_but_hashes_without_them():
    burned = LoanSpend(
        withdrawal_scripts=frozenset({"ab" * 28}),
        minted={_LOAN_TOKEN_POLICY: -1},
        burned_any=frozenset({_LOAN_TOKEN_POLICY}),
    )
    flipped = LoanSpend(
        frozenset({"ab" * 28}),
        {_LOAN_TOKEN_POLICY: 0},
        frozenset({_LOAN_TOKEN_POLICY}),
    )
    assert hash(burned) == hash(flipped)
    assert burned != flipped
    assert burned == LoanSpend(
        frozenset({"ab" * 28}),
        {_LOAN_TOKEN_POLICY: -1},
        frozenset({_LOAN_TOKEN_POLICY}),
    )


def test_a_chained_lifecycle_classifies_with_the_shared_types():
    lifecycle = _ChainedLifecycle()
    spend = LoanSpend(frozenset(), {}, frozenset())
    assert lifecycle.classify_spend(_loan_key(), None, spend) is LoanTransition.REPAID
    successor = _loan_key(out_ref="ee" * 32 + "#0")
    assert lifecycle.classify_spend(_loan_key(), successor, spend) == "modified"


def test_loan_transitions_persist_as_their_values():
    assert [t.value for t in LoanTransition] == [
        "modified",
        "repaid",
        "liquidated",
        "partially_liquidated",
        "migrated",
        "parked",
    ]
    assert LoanTransition("parked") is LoanTransition.PARKED
    assert LoanTransition.PARTIALLY_LIQUIDATED == "partially_liquidated"


def test_supply_options_rank_by_supply_rate_then_market_id():
    book = LendingMarketBook(
        [
            market("no-rate", receipt_unit=_RECEIPT),
            market("low", receipt_unit=_RECEIPT, supply_rate=Fraction(1, 100)),
            market("zero", receipt_unit=_RECEIPT, supply_rate=Fraction(0)),
            market("high-b", receipt_unit=_RECEIPT, supply_rate=Fraction(5, 100)),
            market("high-a", receipt_unit=_RECEIPT, supply_rate=Fraction(5, 100)),
            market("no-receipt", supply_rate=Fraction(9, 100)),
            market(
                "other-token",
                borrow_unit=MIN,
                receipt_unit=_RECEIPT,
                supply_rate=Fraction(9, 100),
            ),
            market(
                "kyc",
                receipt_unit=_RECEIPT,
                supply_rate=Fraction(7, 100),
                permissioned=True,
            ),
        ],
    )
    ranked = [m.market_id for m in book.supply_options("lovelace")]
    assert ranked == ["high-a", "high-b", "low", "zero", "no-rate"]
    with_kyc = book.supply_options("lovelace", include_permissioned=True)
    assert [m.market_id for m in with_kyc] == [
        "kyc",
        "high-a",
        "high-b",
        "low",
        "zero",
        "no-rate",
    ]
    assert [m.market_id for m in book.supply_options(MIN)] == ["other-token"]
    assert book.supply_options(SNEK) == []


def test_borrow_options_stop_at_a_markets_borrow_cap():
    # Headroom: floor(1/2 * (1_000 + 200) - 200) = 400 of the 1_000 held.
    capped = market(
        "capped",
        available_liquidity=1_000,
        max_utilization=Fraction(1, 2),
        total_principal=200,
    )
    assert capped.borrowable_now() == 400
    book = LendingMarketBook([capped, market("open", available_liquidity=1_000)])
    at_cap = book.borrow_options("lovelace", SNEK, 400, 0)
    assert [o.market.market_id for o in at_cap] == ["capped", "open"]
    over_cap = book.borrow_options("lovelace", SNEK, 401, 0)
    assert [o.market.market_id for o in over_cap] == ["open"]
