"""The common lending views: terms, cost, debt, health and storage (offline)."""

from decimal import Decimal
from fractions import Fraction

import pytest

from charli3_dendrite.lending.normalized import BorrowRequest
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import CollateralTerms
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.normalized import Origin
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


def test_a_non_ada_market_needs_a_price_in_its_own_borrow_unit():
    # Prices quoted in lovelace alone do not value SNEK in MIN.
    offer = market(borrow_unit=MIN)
    book = prices((SNEK, "lovelace", 3, 1000), (MIN, "lovelace", 1, 100))
    assert offer.required_collateral(SNEK, 1_000_000, book) is None
    assert offer.required_collateral(SNEK, 1_000_000, prices((SNEK, MIN, 3, 10))) == (
        5_000_000
    )


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
