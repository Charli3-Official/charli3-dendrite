"""The common lending views: terms, cost, debt, health and storage (offline)."""

from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from charli3_dendrite.lending.fluidtokens_v4 import market as ft_market
from charli3_dendrite.lending.liqwid import market as liqwid_market
from charli3_dendrite.lending.normalized import BorrowRequest
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import CollateralTerms
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.normalized import Origin
from charli3_dendrite.lending.normalized import as_parse_error
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.rates import MS_PER_HOUR
from tests.lending.views import MIN
from tests.lending.views import NFT_POLICY
from tests.lending.views import SNEK
from tests.lending.views import SimpleRate
from tests.lending.views import market
from tests.lending.views import prices
from tests.lending.views import request
from tests.lending.views import terms

_YEAR_MS = 8760 * MS_PER_HOUR


def _position(**overrides):
    values = {
        "protocol": "Test",
        "position_id": "p1",
        "origin": Origin("market", "m1"),
        "borrower": Party(PartyKind.TOKEN, "cc" * 28 + "01"),
        "lender": Party(PartyKind.TOKEN, "dd" * 28 + "01"),
        "borrow_unit": "lovelace",
        "principal": 1_000_000_000,
        "installments_paid": 0,
        "collateral": (CollateralHolding(SNEK, 3_000_000_000),),
        "rate_model": SimpleRate(1000),
        "liquidation_ltv": Fraction(4, 5),
        "on_default": DefaultRule.PRICE_LIQUIDATION,
        "opened_ms": 1_000_000,
    }
    values.update(overrides)
    return LendingPosition(**values)


@pytest.mark.parametrize(
    ("max_ltv", "fixed_ratio"),
    [(None, None), (Fraction(1, 2), Fraction(2))],
)
def test_terms_need_exactly_one_borrowing_limit(max_ltv, fixed_ratio):
    with pytest.raises(ValueError, match="exactly one"):
        terms(max_ltv=max_ltv, fixed_ratio=fixed_ratio)


def test_policy_wide_terms_accept_any_token_of_their_policy():
    wide = terms(NFT_POLICY, policy_wide=True)
    assert wide.accepts(NFT_POLICY + "01")
    assert wide.accepts(NFT_POLICY)
    assert not wide.accepts("c" * 56 + "01")
    assert not terms(SNEK).accepts(SNEK[:56] + "00")


def test_an_exact_collateral_match_wins_over_a_policy_wide_one():
    exact = terms(NFT_POLICY + "01", max_ltv=Fraction(1, 2))
    wide = terms(NFT_POLICY, policy_wide=True)
    offer = market(collateral=(wide, exact))
    assert offer.terms_for(NFT_POLICY + "01") is exact
    assert offer.terms_for(NFT_POLICY + "02") is wide
    assert offer.terms_for(MIN) is None


def test_required_collateral_at_a_fixed_ratio_needs_no_price():
    offer = market(collateral=(terms(max_ltv=None, fixed_ratio=Fraction(875, 2)),))
    assert offer.required_collateral(SNEK, 1_000_001) == 437_500_438


def test_required_collateral_at_a_max_ltv_prices_the_collateral():
    # SNEK at 3/1000 lovelace: 1 ADA needs 1_000_000 / (2/3 * 3/1000) SNEK.
    book = prices((SNEK, "lovelace", 3, 1000))
    assert market().required_collateral(SNEK, 1_000_000, book) == 500_000_000
    assert market().required_collateral(SNEK, 1_000_001, book) == 500_000_500
    assert market().required_collateral(SNEK, 1_000_000) is None
    assert (
        market().required_collateral(SNEK, 1, prices((SNEK, "lovelace", 0, 1))) is None
    )
    assert market().required_collateral(MIN, 1_000_000, book) is None


def test_a_non_ada_market_crosses_lovelace_prices():
    # SNEK at 3/1000 and MIN at 1/100 lovelace: a SNEK is worth 3/10 MIN.
    offer = market(borrow_unit=MIN)
    both = prices((SNEK, "lovelace", 3, 1000), (MIN, "lovelace", 1, 100))
    assert offer.required_collateral(SNEK, 1_000_000, both) == 5_000_000
    direct = prices(
        (SNEK, MIN, 3, 20),
        (SNEK, "lovelace", 3, 1000),
        (MIN, "lovelace", 1, 100),
    )
    assert offer.required_collateral(SNEK, 1_000_000, direct) == 10_000_000
    # Without the borrow unit's own price there is nothing to cross.
    alone = prices((SNEK, "lovelace", 3, 1000))
    assert offer.required_collateral(SNEK, 1_000_000, alone) is None


def test_ada_collateral_is_worth_one_lovelace_per_lovelace():
    offer = market(borrow_unit=MIN, collateral=(terms("lovelace"),))
    book = prices((MIN, "lovelace", 1, 100))
    # 1 MIN = 1/100 lovelace, so 1_000_000 MIN is 10_000 lovelace of value.
    assert offer.required_collateral("lovelace", 1_000_000, book) == 15_000


def test_a_policy_price_values_each_token_of_the_policy():
    position = _position(
        collateral=(
            CollateralHolding(NFT_POLICY + "01", 1),
            CollateralHolding(NFT_POLICY + "02", 1),
        ),
    )
    floor = prices((NFT_POLICY, "lovelace", 900_000_000, 1))
    assert position.collateral_value(floor) == 1_800_000_000
    assert position.health_factor(floor, position.opened_ms) == Decimal("1.44")
    own = prices(
        (NFT_POLICY, "lovelace", 900_000_000, 1),
        (NFT_POLICY + "01", "lovelace", 1, 1),
    )
    assert position.collateral_value(own) == 900_000_001


def test_the_headline_rate_is_the_rate_models():
    assert market().borrow_rate == Fraction(1, 20)


def test_an_open_ended_position_has_no_dates():
    position = _position()
    assert position.expires_ms is None
    assert position.next_payment_due_ms is None
    assert position.is_late(10**15) is False


def test_debt_and_interest_come_from_the_rate_model():
    position = _position()
    now = position.opened_ms + _YEAR_MS
    assert position.current_debt(now) == 1_100_000_000
    assert position.interest_accrued(now) == 100_000_000


def test_health_factor_values_the_collateral_in_the_borrow_unit():
    position = _position()
    now = position.opened_ms  # debt == principal
    book = prices((SNEK, "lovelace", 1, 2))
    # 3_000 ADA of SNEK at 1/2 = 1_500 ADA; * 4/5 = 1_200 ADA over 1_000 ADA.
    assert position.collateral_value(book) == 1_500_000_000
    assert position.health_factor(book, now) == Decimal("1.2")
    assert position.is_liquidatable(book, now) is False
    low = prices((SNEK, "lovelace", 1, 4))
    assert position.health_factor(low, now) == Decimal("0.6")
    assert position.is_liquidatable(low, now) is True


def test_an_unpriced_holding_is_never_flagged_liquidatable():
    position = _position(
        collateral=(
            CollateralHolding(SNEK, 1),
            CollateralHolding(MIN, 1),
        ),
    )
    book = prices((SNEK, "lovelace", 1, 1))
    assert position.is_liquidatable(book, position.opened_ms) is False


def test_a_loan_without_price_liquidation_is_infinitely_healthy():
    position = _position(liquidation_ltv=None)
    book = prices((SNEK, "lovelace", 1, 10**9))
    assert position.health_factor(book, position.opened_ms) == Decimal("Infinity")
    assert position.is_liquidatable(book, position.opened_ms) is False


def test_a_fixed_ratio_request_takes_a_range_up_to_its_cap():
    assert request().principal_range() == (1_000_000, 1_000_000_000)
    assert request(principal_max=999_999).principal_range() is None


def test_a_priced_request_takes_one_amount_and_ignores_its_cap():
    priced = request(
        principal_limit=terms(),
        principal_max=1,
    )
    book = prices((SNEK, "lovelace", 3, 1000))
    # 875_000_000 SNEK at 3/1000 lovelace, times the 2/3 LTV.
    assert priced.principal_range(book) == (1_750_000, 1_750_000)
    assert priced.principal_range() is None


def test_a_parse_error_keeps_its_reason():
    error = LendingParseError("zero divider")
    assert isinstance(error, ValueError)
    assert error.reason == "zero divider"


def _feed() -> OracleRef:
    return OracleRef(
        source=OracleSource.FLUID_AGGREGATED,
        token=SNEK,
        quote="lovelace",
        feed_policy="ee" * 28,
        feed_name="6f7261636c65",
    )


@pytest.mark.parametrize(
    "view",
    [
        market(collateral=(terms(price_source=_feed()),)),
        market(lender=None, supply_rate=Fraction(3, 100), utilization=Fraction(1, 2)),
        _position(),
        _position(origin=None, lender=None, liquidation_ltv=None),
        request(),
    ],
)
def test_every_view_survives_its_dict_form(view):
    assert type(view).from_dict(view.to_dict()) == view


def test_views_deduplicate_even_with_an_oracle_feed():
    fed = market(collateral=(terms(price_source=_feed()),))
    assert len({fed, market(collateral=(terms(price_source=_feed()),))}) == 1
    assert len({_position(), request(), fed}) == 3


def test_a_pooled_market_with_several_collaterals_fits():
    # A pooled protocol: no single lender, a receipt token, a supply rate and a
    # utilization, several collaterals each with a weight and a liquidation discount.
    q_ada, q_min = "a1" * 28 + "71", "a2" * 28 + "71"
    pooled = market(
        lender=None,
        receipt_unit="a3" * 28 + "71",
        supply_rate=Fraction(21, 1000),
        utilization=Fraction(57, 100),
        collateral=(
            terms(
                q_ada,
                max_ltv=Fraction(3, 4),
                liquidation_ltv=Fraction(4, 5),
                liquidation_penalty=None,
                liquidation_discount=Fraction(1, 20),
            ),
            terms(
                q_min,
                max_ltv=Fraction(1, 2),
                liquidation_ltv=Fraction(3, 5),
                liquidation_penalty=None,
                liquidation_discount=Fraction(1, 10),
            ),
        ),
    )
    loan = _position(
        origin=Origin("market", pooled.market_id),
        borrower=Party(PartyKind.KEY, "ab" * 28),
        lender=None,
        collateral=(CollateralHolding(q_ada, 10), CollateralHolding(q_min, 20)),
    )
    assert LendingMarket.from_dict(pooled.to_dict()) == pooled
    assert LendingPosition.from_dict(loan.to_dict()) == loan
    assert pooled.terms_for(q_min).liquidation_discount == Fraction(1, 10)


def test_collateral_terms_survive_their_dict_form_without_a_feed():
    plain = terms(max_ltv=None, fixed_ratio=Fraction(5, 2), liquidation_ltv=None)
    assert CollateralTerms.from_dict(plain.to_dict()) == plain


def test_a_request_dict_names_its_rate_model():
    record = request().to_dict()
    assert record["rate_model"] == {
        "protocol": "Test",
        "kind": "variable",
        "params": {"rate": 700},
    }
    assert BorrowRequest.from_dict(record).rate_model == SimpleRate(700)


# A market and a position exactly as `to_dict` wrote them before the optional
# holding weight, term weight, receipt rate and borrow-cap fields existed.
_PRE_CHANGE_MARKET = {
    "protocol": "Test",
    "market_id": "m1",
    "borrow_unit": "lovelace",
    "lender": {"kind": "key", "identifier": "aa" * 28, "detail": None},
    "available_liquidity": 1_000_000_000,
    "rate_model": {"protocol": "Test", "kind": "variable", "params": {"rate": 500}},
    "supply_rate": None,
    "receipt_unit": None,
    "utilization": None,
    "collateral": [
        {
            "unit": SNEK,
            "policy_wide": False,
            "max_ltv": {"num": 2, "den": 3},
            "fixed_ratio": None,
            "liquidation_ltv": {"num": 4, "den": 5},
            "liquidation_penalty": {"num": 1, "den": 10},
            "liquidation_discount": None,
            "equity_to_borrower": True,
            "price_source": None,
        },
    ],
    "on_default": "price_liquidation",
    "permissioned": False,
}

_PRE_CHANGE_POSITION = {
    "protocol": "Test",
    "position_id": "p1",
    "origin": {"kind": "market", "identifier": "m1"},
    "borrower": {"kind": "token", "identifier": "cc" * 28 + "01", "detail": None},
    "lender": {"kind": "token", "identifier": "dd" * 28 + "01", "detail": None},
    "borrow_unit": "lovelace",
    "principal": 1_000_000_000,
    "installments_paid": 0,
    "collateral": [{"unit": SNEK, "amount": 3_000_000_000}],
    "rate_model": {"protocol": "Test", "kind": "variable", "params": {"rate": 1000}},
    "liquidation_ltv": {"num": 4, "den": 5},
    "on_default": "price_liquidation",
    "opened_ms": 1_000_000,
}


def test_a_market_stored_before_the_optional_fields_loads_and_reserialises():
    loaded = LendingMarket.from_dict(_PRE_CHANGE_MARKET)
    assert loaded == market()
    assert loaded.receipt_rate is None
    assert loaded.max_utilization is None
    assert loaded.total_principal is None
    assert loaded.collateral[0].weight is None
    assert loaded.to_dict() == _PRE_CHANGE_MARKET


def test_a_position_stored_before_holding_weights_loads_and_reserialises():
    loaded = LendingPosition.from_dict(_PRE_CHANGE_POSITION)
    assert loaded == _position()
    assert loaded.collateral[0].liquidation_ltv is None
    assert loaded.to_dict() == _PRE_CHANGE_POSITION


def test_a_holding_emits_its_liquidation_ltv_only_when_set():
    plain = CollateralHolding(SNEK, 5)
    assert plain.to_dict() == {"unit": SNEK, "amount": 5}
    weighted = CollateralHolding(SNEK, 5, Fraction(3, 4))
    assert weighted.to_dict() == {
        "unit": SNEK,
        "amount": 5,
        "liquidation_ltv": {"num": 3, "den": 4},
    }
    assert CollateralHolding.from_dict(weighted.to_dict()) == weighted
    assert weighted != plain


def test_terms_emit_their_weight_only_when_set():
    assert "weight" not in terms().to_dict()
    weighted = terms(weight=Fraction(81, 100_000_000))
    assert weighted.to_dict()["weight"] == {"num": 81, "den": 100_000_000}
    assert CollateralTerms.from_dict(weighted.to_dict()) == weighted
    assert weighted != terms()


def test_a_market_emits_receipt_rate_and_borrow_cap_only_when_set():
    record = market().to_dict()
    for key in ("receipt_rate", "max_utilization", "total_principal"):
        assert key not in record
    pooled = market(
        lender=None,
        receipt_unit="a3" * 28,
        receipt_rate=Fraction(2_134, 100_000),
        max_utilization=Fraction(7, 8),
        total_principal=3_000_000,
        collateral=(terms(weight=Fraction(9, 10)),),
    )
    record = pooled.to_dict()
    assert record["receipt_rate"] == {"num": 1_067, "den": 50_000}
    assert record["max_utilization"] == {"num": 7, "den": 8}
    assert record["total_principal"] == 3_000_000
    assert LendingMarket.from_dict(record) == pooled


def test_without_a_borrow_cap_all_liquidity_is_borrowable():
    assert market().borrowable_now() == 1_000_000_000
    # a recorded principal alone does not cap anything
    assert market(total_principal=5_000_000_000).borrowable_now() == 1_000_000_000


@pytest.mark.parametrize(
    ("available", "principal", "cap", "expected"),
    [
        # a zero cap disables borrowing
        (1_000_000, 0, Fraction(0), 0),
        # principal already above the cap
        (1_000, 9_000, Fraction(7, 8), 0),
        # headroom below the available liquidity
        (1_000_000, 3_000_000, Fraction(4, 5), 200_000),
        # headroom above the available liquidity: the liquidity bounds it
        (1_000_000, 1_000_000, Fraction(3, 2), 1_000_000),
        # no principal recorded counts as none lent
        (1_000_000, None, Fraction(4, 5), 800_000),
    ],
)
def test_a_borrow_cap_bounds_what_is_borrowable_now(
    available, principal, cap, expected
):
    offer = market(
        available_liquidity=available,
        max_utilization=cap,
        total_principal=principal,
    )
    assert offer.borrowable_now() == expected


def test_a_zero_max_ltv_gives_no_required_collateral():
    offer = market(collateral=(terms(max_ltv=Fraction(0)),))
    book = prices((SNEK, "lovelace", 1, 500))
    assert offer.required_collateral(SNEK, 1_000_000, book) is None
    # the fixed-ratio path is unaffected
    fixed = market(collateral=(terms(max_ltv=None, fixed_ratio=Fraction(2)),))
    assert fixed.required_collateral(SNEK, 1_000_000) == 2_000_000


@dataclass(frozen=True)
class _PooledRate(SimpleRate):
    """Simple interest whose repayment adds a fixed minimum the health check ignores.

    Rebinding takes the market model's current rate, as a pooled market's loans do.
    """

    minimum: int = 0

    def outstanding(
        self,
        principal: int,
        *,
        opened_ms: int,
        now_ms: int,
        installments_paid: int,
    ) -> int:
        unfloored = self.liquidation_debt(
            principal,
            opened_ms=opened_ms,
            now_ms=now_ms,
            installments_paid=installments_paid,
        )
        return unfloored + self.minimum

    def liquidation_debt(
        self,
        principal: int,
        *,
        opened_ms: int,
        now_ms: int,
        installments_paid: int,
    ) -> int:
        return SimpleRate.outstanding(
            self,
            principal,
            opened_ms=opened_ms,
            now_ms=now_ms,
            installments_paid=installments_paid,
        )

    def rebind(self, market_model):
        return replace(self, rate=market_model.rate)


def test_the_liquidation_debt_comes_from_the_rate_model():
    position = _position(rate_model=_PooledRate(1000, minimum=500_000_000))
    now = position.opened_ms + _YEAR_MS
    assert position.current_debt(now) == 1_600_000_000
    assert position.liquidation_debt(now) == 1_100_000_000
    assert _position().liquidation_debt(now) == _position().current_debt(now)


def test_weighted_collateral_values_each_holding_at_its_own_ltv():
    unpriced = NFT_POLICY + "01"
    position = _position(
        collateral=(
            CollateralHolding(SNEK, 3_000_000_000, Fraction(4, 5)),
            CollateralHolding(MIN, 1_000_000_000),
            CollateralHolding(unpriced, 7, Fraction(1, 2)),
        ),
    )
    book = prices((SNEK, "lovelace", 1, 2), (MIN, "lovelace", 1, 3))
    # SNEK: 3_000 ADA * 1/2 * 4/5; MIN has no LTV of its own, so counts in full.
    assert position.weighted_collateral(book) == (
        Fraction(1_200_000_000),
        Fraction(1_000_000_000, 3),
        None,
    )


def test_health_with_holding_ltvs_weighs_each_holding_exactly():
    position = _position(
        liquidation_ltv=Fraction(9, 10),
        collateral=(
            CollateralHolding(SNEK, 3_000_000_000, Fraction(4, 5)),
            CollateralHolding(MIN, 1_000_000_000),
            CollateralHolding(NFT_POLICY + "01", 7, Fraction(1, 2)),
        ),
    )
    book = prices((SNEK, "lovelace", 1, 2), (MIN, "lovelace", 1, 3))
    # (1_200 + 1_000/3) ADA * 9/10 over 1_000 ADA; the unpriced holding counts 0.
    weighted = Fraction(1_200_000_000) + Fraction(1_000_000_000, 3)
    assert weighted * Fraction(9, 10) / 1_000_000_000 == Fraction(69, 50)
    assert position.health_factor(book, position.opened_ms) == Decimal("1.38")


def test_health_with_holding_ltvs_is_measured_against_the_liquidation_debt():
    position = _position(
        rate_model=_PooledRate(1000, minimum=500_000_000),
        liquidation_ltv=Fraction(1),
        collateral=(CollateralHolding(SNEK, 3_000_000_000, Fraction(4, 5)),),
    )
    book = prices((SNEK, "lovelace", 1, 2))
    # 1_200 ADA weighted over the 1_000 ADA liquidation debt, not the 1_500 ADA owed.
    assert position.health_factor(book, position.opened_ms) == Decimal("1.2")


def test_health_without_holding_ltvs_keeps_the_single_ltv_expression():
    position = _position(rate_model=_PooledRate(1000, minimum=500_000_000))
    book = prices((SNEK, "lovelace", 1, 2))
    # 1_500 ADA * 4/5 over the 1_500 ADA owed: the floored debt, as before.
    assert position.health_factor(book, position.opened_ms) == Decimal("0.8")


def test_health_with_holding_ltvs_is_infinite_without_debt_or_a_position_ltv():
    weighted = (CollateralHolding(SNEK, 3_000_000_000, Fraction(4, 5)),)
    book = prices((SNEK, "lovelace", 1, 2))
    no_debt = _position(principal=0, collateral=weighted)
    assert no_debt.health_factor(book, no_debt.opened_ms) == Decimal("Infinity")
    no_ltv = _position(liquidation_ltv=None, collateral=weighted)
    assert no_ltv.health_factor(book, no_ltv.opened_ms) == Decimal("Infinity")


@pytest.mark.parametrize(
    "elsewhere",
    [
        market(protocol="Other"),
        market("m2"),
    ],
)
def test_at_market_refuses_a_market_the_loan_is_not_from(elsewhere):
    with pytest.raises(ValueError, match="not opened from market"):
        _position().at_market(elsewhere)


@pytest.mark.parametrize("origin", [None, Origin("request", "m1")])
def test_at_market_refuses_a_loan_not_opened_from_a_market(origin):
    with pytest.raises(ValueError, match="not opened from market"):
        _position(origin=origin).at_market(market())


def test_at_market_rebinds_the_rate_model_to_the_market():
    position = _position(rate_model=_PooledRate(1000, minimum=7))
    rebound = position.at_market(market(rate_model=_PooledRate(2500)))
    assert rebound.rate_model == _PooledRate(2500, minimum=7)
    assert position.rate_model == _PooledRate(1000, minimum=7)
    plain = _position()
    assert plain.at_market(market()).rate_model is plain.rate_model


def test_at_market_refreshes_each_holding_ltv_from_the_market_terms():
    fixed = terms(MIN, max_ltv=None, fixed_ratio=Fraction(2), liquidation_ltv=None)
    pooled = market(collateral=(terms(liquidation_ltv=Fraction(3, 4)), fixed))
    position = _position(
        collateral=(
            CollateralHolding(SNEK, 3_000_000_000, Fraction(1, 2)),
            CollateralHolding(MIN, 1_000_000_000, Fraction(1, 3)),
        ),
    )
    rebound = position.at_market(pooled)
    assert rebound.collateral == (
        CollateralHolding(SNEK, 3_000_000_000, Fraction(3, 4)),
        CollateralHolding(MIN, 1_000_000_000, Fraction(1, 3)),
    )
    assert rebound == replace(position, collateral=rebound.collateral)


def test_a_holding_its_market_has_no_terms_for_counts_for_nothing():
    dropped = NFT_POLICY + "01"  # no terms in the market, and no price
    position = _position(
        liquidation_ltv=Fraction(1),
        collateral=(
            CollateralHolding(SNEK, 3_000_000_000, Fraction(4, 5)),
            CollateralHolding(MIN, 1_000_000_000, Fraction(1, 2)),
            CollateralHolding(dropped, 5),
        ),
    )
    rebound = position.at_market(market())  # accepts SNEK only
    assert [holding.liquidation_ltv for holding in rebound.collateral] == [
        Fraction(4, 5),
        Fraction(0),
        Fraction(0),
    ]
    book = prices((SNEK, "lovelace", 1, 2), (MIN, "lovelace", 1, 3))
    assert rebound.weighted_collateral(book) == (
        Fraction(1_200_000_000),
        Fraction(0),
        None,
    )
    health = rebound.health_factor(book, rebound.opened_ms)
    assert health == Decimal("1.2")
    assert health.is_finite()
    assert rebound.is_liquidatable(book, rebound.opened_ms) is False
    assert LendingPosition.from_dict(rebound.to_dict()) == rebound
    stranded = _position(
        liquidation_ltv=Fraction(1),
        collateral=(CollateralHolding(dropped, 5),),
    ).at_market(market())
    assert stranded.weighted_collateral(book) == (None,)
    assert stranded.health_factor(book, stranded.opened_ms) == Decimal(0)


def test_as_parse_error_prefixes_a_foreign_error():
    cause = ZeroDivisionError("division by zero")
    error = as_parse_error("pool abc#0", cause)
    assert type(error) is LendingParseError
    assert str(error) == "pool abc#0: division by zero"


def test_as_parse_error_passes_a_parse_error_through():
    cause = LendingParseError("a rate needs a positive denominator")
    assert as_parse_error("pool abc#0", cause) is cause


def test_both_protocols_wrap_through_the_shared_helper():
    assert ft_market.as_parse_error is as_parse_error
    assert liqwid_market.as_parse_error is as_parse_error
    assert not hasattr(ft_market, "_converted")
