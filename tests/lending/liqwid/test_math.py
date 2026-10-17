"""Liqwid V2 math: exact arithmetic, then the captured mainnet records against the
Liqwid API values captured with them (``fixtures/records.json.gz``,
``fixtures/api_snapshot.json``) and against settled mainnet transactions."""

from __future__ import annotations

from decimal import Decimal
from decimal import localcontext
from fractions import Fraction
from typing import Any

import pytest

from charli3_dendrite.lending import math as lending_math
from charli3_dendrite.lending.liqwid import math as lqm
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.market import market_to_view
from charli3_dendrite.lending.liqwid.oracles import liqwid_price_map
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.normalized import Origin
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.qtoken import receipt_prices
from tests.lending.liqwid.records import API
from tests.lending.liqwid.records import live_context
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.liqwid.records import spend_of

DEPLOYMENT = default_deployment()
POLICY_HEX_LEN = 56
OWNER = "00" * 28


def _state(**fields: Any) -> MarketStateDatum:
    values: dict[str, Any] = {
        "cash": 0,
        "reserve": 0,
        "qtokens": 0,
        "principal": 0,
        "interest": 0,
        "index": 1,
        "rate": (0, 1),
        "last_interest_ms": 0,
        "last_batch_ms": 0,
        "qtoken_rate": (1, 50),
        "min_ada": 3_000_000,
    }
    values.update(fields)
    return MarketStateDatum(**values)


def _loan(principal: int, interest: int, min_interest: int, index: int) -> LoanDatum:
    return LoanDatum(
        owner=OWNER,
        principal=principal,
        interest=interest,
        min_interest=min_interest,
        index=index,
    )


# ---------------------------------------------------------------- exact arithmetic


def test_utilization_is_borrowed_over_total_and_zero_on_an_empty_market():
    state = _state(cash=60, principal=30, interest=10)
    assert lqm.utilization(state) == Fraction(40, 100)
    assert lqm.utilization(_state()) == 0


KINKED = ((1, 100), (1, 10), (1, 1), (4, 5))  # base, multiplier, jump, kink


@pytest.mark.parametrize(
    ("util", "expected"),
    [
        pytest.param(Fraction(0), Fraction(1, 100), id="idle"),
        pytest.param(Fraction(1, 2), Fraction(1, 100) + Fraction(1, 20), id="below"),
        pytest.param(Fraction(4, 5), Fraction(1, 100) + Fraction(8, 100), id="at-kink"),
        pytest.param(
            Fraction(9, 10),
            Fraction(1, 100) + Fraction(9, 100) + Fraction(1, 10),
            id="above",
        ),
    ],
)
def test_borrow_rate_is_the_kinked_model(util, expected):
    assert lqm.borrow_rate(KINKED, util) == expected


def test_borrow_rate_rejects_a_model_without_four_terms():
    with pytest.raises(ValueError, match="4"):
        lqm.borrow_rate(KINKED[:3], Fraction(1, 2))


def test_borrow_apy_compounds_per_period_to_sixty_digits():
    # One period per year: the yearly rate is the period rate itself, exactly.
    assert lqm.borrow_apy(Fraction(1, 8), period_ms=lqm.YEAR_MS) == Decimal("0.125")
    assert lqm.borrow_apy(Fraction(0)) == 0
    rate = Fraction(1, 1_000_000)
    with localcontext() as ctx:
        ctx.prec = 60
        expected = (1 + Decimal(1) / Decimal(1_000_000)) ** 15_768 - 1
    assert lqm.borrow_apy(rate) == expected
    assert lqm.INTEREST_PERIOD_MS == 2_000_000


def test_borrow_apy_rejects_a_bad_period_or_rate():
    with pytest.raises(ValueError, match="period"):
        lqm.borrow_apy(Fraction(1, 8), period_ms=0)
    with pytest.raises(ValueError, match="negative"):
        lqm.borrow_apy(Fraction(-1, 8))


def test_supply_apy_is_the_suppliers_share_of_the_borrow_apy():
    apy = lqm.supply_apy(
        Fraction(1, 8),
        Fraction(1, 2),
        (10, 8, 0, 0),
        period_ms=lqm.YEAR_MS,
    )
    assert apy == Decimal("0.05")
    with pytest.raises(ValueError, match="income ratio"):
        lqm.supply_apy(Fraction(1, 8), Fraction(1, 2), (0, 8, 0, 0))


def test_apy_fraction_carries_the_decimal_exactly():
    assert lqm.apy_fraction(Decimal("0.0526261384")) == Fraction(526_261_384, 10**10)
    assert lqm.apy_fraction(Decimal("5.26E-2")) == Fraction(526, 10_000)


def test_qtoken_rate_counts_the_suppliers_share_of_interest():
    state = _state(cash=100, qtokens=50, principal=40, interest=10)
    # (100 + 40 + floor(10 * 8 / 10)) / 50
    assert lqm.qtoken_rate(state, (10, 8, 0, 0)) == Fraction(148, 50)
    # floor of the interest share: 7 * 8 / 10 = 5.6 -> 5
    rounded = _state(cash=1, qtokens=2, interest=7)
    assert lqm.qtoken_rate(rounded, (10, 8)) == Fraction(6, 2)
    # no qTokens in circulation: the recorded rate stands
    assert lqm.qtoken_rate(_state(qtoken_rate=(1, 50)), (10, 8)) == Fraction(1, 50)


def test_supply_mint_rounds_down():
    assert lqm.supply_mint(10_000_000_000, Fraction(1, 50)) == 500_000_000_000
    rate = Fraction(117_344_965, 5_750_000_068)
    minted = lqm.supply_mint(10_000_000_000, rate)
    assert minted * rate <= 10_000_000_000 < (minted + 1) * rate
    with pytest.raises(ValueError, match="rate"):
        lqm.supply_mint(1, Fraction(0))


def test_min_interest_at_borrow_rounds_up():
    assert lqm.min_interest_at_borrow(1_250, Fraction(1, 1250)) == 1
    assert lqm.min_interest_at_borrow(1_251, Fraction(1, 1250)) == 2
    assert lqm.min_interest_at_borrow(0, Fraction(1, 1250)) == 0


def test_payoff_is_the_indexed_debt_floored_at_the_min_interest():
    # (1000 + 0) * 3 / 2 = 1500, above 1000 + 10
    accrued = _loan(1_000, 0, 10, 2)
    assert lqm.liquidation_debt(accrued, 3) == 1_500
    assert lqm.payoff(accrued, 3) == 1_500
    # ceil((1000 + 1) * 1001 / 1000) = 1003, below 1000 + 10: the floor binds
    fresh = _loan(1_000, 1, 10, 1_000)
    assert lqm.liquidation_debt(fresh, 1_001) == 1_003
    assert lqm.payoff(fresh, 1_001) == 1_010
    # a zero loan index carries no accrual
    assert lqm.liquidation_debt(_loan(500, 5, 1, 0), 7) == 505
    assert lqm.payoff(_loan(500, 5, 1, 0), 7) == 505


def test_health_and_ltv_weigh_value_against_debt():
    holdings = [
        lqm.WeightedHolding(
            amount=1_000,
            rate=Fraction(1, 50),
            price=Fraction(2),
            weight=Fraction(4, 5),
        ),
        lqm.WeightedHolding(
            amount=300,
            rate=Fraction(1),
            price=Fraction(1, 3),
            weight=Fraction(0),
        ),
    ]
    # values 40 and 100; weighted 32 and 0; debt value 20 * 1
    assert holdings[0].value == 40
    assert holdings[0].weighted_value == 32
    assert lqm.health(holdings, 20, Fraction(1)) == Fraction(32, 20)
    assert lqm.ltv(holdings, 20, Fraction(1)) == Fraction(20, 140)
    assert lqm.ltv(holdings, 0, Fraction(1)) == 0
    assert lqm.health([], 20, Fraction(1)) == 0


def test_health_and_ltv_reject_what_they_cannot_score():
    holding = lqm.WeightedHolding(1, Fraction(1), Fraction(1), Fraction(1))
    with pytest.raises(ValueError, match="debt"):
        lqm.health([holding], 0, Fraction(1))
    with pytest.raises(ValueError, match="price"):
        lqm.health([holding], 1, Fraction(0))
    with pytest.raises(ValueError, match="collateral"):
        lqm.ltv([], 1, Fraction(1))
    with pytest.raises(ValueError, match="debt"):
        lqm.ltv([holding], -1, Fraction(1))


def test_liquidation_repay_and_seized_value():
    # unfloored debt ceil(1001 * 1) = 1001; half of it rounds down
    loan = _loan(1_000, 1, 500, 1)
    assert lqm.liquidation_repay(loan, 1, Fraction(1, 2)) == 500
    assert lqm.liquidation_repay(loan, 1, Fraction(1)) == 1_001
    assert lqm.seized_value(900, Fraction(9, 10)) == 1_000
    with pytest.raises(ValueError, match="close factor"):
        lqm.liquidation_repay(loan, 1, Fraction(0))
    with pytest.raises(ValueError, match="discount"):
        lqm.seized_value(900, Fraction(0))


def test_borrow_headroom_is_the_shared_implementation():
    assert lqm.borrow_headroom is lending_math.borrow_headroom
    assert "borrow_headroom" in lqm.__all__


# ------------------------------------------------- captured snapshot against the API


def _parsed(kind: str) -> list[tuple[dict[str, Any], Any]]:
    """Live records of ``kind`` that parse as genuine, with their fixture entry."""
    out = []
    for rec in records_of(kind, live=True):
        parsed = parse_utxo(record_info(rec), DEPLOYMENT)
        if parsed is not None:
            out.append((rec, parsed))
    return out


@pytest.fixture(scope="module")
def states() -> dict[str, MarketStateDatum]:
    found: dict[str, MarketStateDatum] = {}
    for _, parsed in _parsed("market_state"):
        assert parsed.kind is LiqwidKind.MARKET_STATE
        assert parsed.market_id not in found, parsed.market_id
        found[parsed.market_id] = parsed.datum
    return found


@pytest.fixture(scope="module")
def params() -> dict[str, MarketParamsDatum]:
    """The newest live MarketParams of each market."""
    newest: dict[str, tuple[int, MarketParamsDatum]] = {}
    for rec, parsed in _parsed("market_params"):
        assert parsed.kind is LiqwidKind.MARKET_PARAMS
        seen = newest.get(parsed.market_id)
        if seen is None or rec["slot"] > seen[0]:
            newest[parsed.market_id] = (rec["slot"], parsed.datum)
    return {market_id: datum for market_id, (_, datum) in newest.items()}


@pytest.fixture(scope="module")
def weights() -> dict[tuple[str, str], CollateralParamsDatum]:
    """The newest live CollateralParams per (borrow market, collateral unit)."""
    newest: dict[tuple[str, str], tuple[int, CollateralParamsDatum]] = {}
    for rec, parsed in _parsed("collateral_params"):
        assert parsed.kind is LiqwidKind.COLLATERAL_PARAMS
        key = (parsed.market_id, parsed.datum.collateral)
        seen = newest.get(key)
        if seen is None or rec["slot"] > seen[0]:
            newest[key] = (rec["slot"], parsed.datum)
    return {key: datum for key, (_, datum) in newest.items()}


@pytest.fixture(scope="module")
def prices() -> PriceMap:
    feeds = [
        parse_feed(record_info(rec), DEPLOYMENT)
        for rec in records_of("oracle", live=True)
    ]
    return liqwid_price_map(feed for feed in feeds if feed is not None)


@pytest.fixture(scope="module")
def loans() -> dict[str, tuple[str, LoanDatum, dict[str, int]]]:
    """out_ref -> (market_id, loan datum, assets) for every live genuine loan."""
    found = {}
    for rec, parsed in _parsed("loan"):
        assert parsed.kind is LiqwidKind.LOAN
        found[rec["out_ref"]] = (parsed.market_id, parsed.datum, dict(rec["assets"]))
    return found


def _lovelace_price(prices: PriceMap, unit: str) -> Fraction | None:
    """Lovelace per base unit of ``unit`` (Ada itself is 1)."""
    if unit == "lovelace":
        return Fraction(1)
    price = prices.get(unit)
    return None if price is None else Fraction(price.num, price.denom)


def _qtoken_holdings(assets: dict[str, int]) -> list[tuple[str, int]]:
    """The qTokens a loan locks: bare 56-hex policies of a known qToken."""
    return sorted(
        (unit, amount)
        for unit, amount in assets.items()
        if len(unit) == POLICY_HEX_LEN and DEPLOYMENT.market_of_qtoken(unit) is not None
    )


def _market_named(name: str) -> MarketComponents:
    matches = [market for market in DEPLOYMENT.markets if market.name == name]
    assert len(matches) == 1, name
    return matches[0]


def test_payoff_and_unfloored_interest_match_the_api_on_every_live_loan(loans, states):
    assert set(loans) == set(API["loans"])
    assert len(loans) == 817
    floor_binds = 0
    for out_ref, (market_id, loan, _) in loans.items():
        api = API["loans"][out_ref]
        scale = 10 ** DEPLOYMENT.market(market_id).decimals
        index = states[market_id].index
        debt = lqm.payoff(loan, index)
        unfloored = lqm.liquidation_debt(loan, index)
        assert debt == round(api["debt"] * scale), out_ref
        assert unfloored - loan.principal == round(api["interest"] * scale), out_ref
        assert unfloored <= debt
        floor_binds += debt > unfloored
    assert floor_binds == 71


def test_health_and_ltv_match_the_api_on_every_servable_loan(
    loans,
    states,
    params,
    weights,
    prices,
):
    scored = 0
    for out_ref, (market_id, loan, assets) in loans.items():
        held = _qtoken_holdings(assets)
        if loan.principal == 0 or not held:
            continue
        threshold = Fraction(*params[market_id].liquidation_threshold)
        holdings = []
        for unit, amount in held:
            collateral = DEPLOYMENT.market_of_qtoken(unit)
            price = _lovelace_price(prices, collateral.underlying)
            assert price is not None, (out_ref, unit)
            terms = weights.get((market_id, unit))
            weight = Fraction(0) if terms is None else Fraction(*terms.weight)
            holdings.append(
                lqm.WeightedHolding(
                    amount=amount,
                    rate=Fraction(*states[collateral.market_id].qtoken_rate),
                    price=price,
                    weight=weight * threshold,
                ),
            )
        borrow_price = _lovelace_price(prices, DEPLOYMENT.market(market_id).underlying)
        assert borrow_price is not None, out_ref
        debt = lqm.liquidation_debt(loan, states[market_id].index)
        api = API["loans"][out_ref]
        assert float(lqm.health(holdings, debt, borrow_price)) == pytest.approx(
            api["healthFactor"],
            rel=1e-9,
        ), out_ref
        assert float(lqm.ltv(holdings, debt, borrow_price)) == pytest.approx(
            api["LTV"],
            rel=1e-9,
        ), out_ref
        scored += 1
    assert scored == 565


def test_market_rates_and_supply_side_match_the_api(states, params):
    assert set(states) == set(API["markets"])
    assert len(states) == 35
    exact_supply = 0
    staking_terms = []
    for market_id, state in states.items():
        api = API["markets"][market_id]
        market = DEPLOYMENT.market(market_id)
        market_params = params[market_id]
        util = lqm.utilization(state)
        rate = Fraction(*state.rate)
        assert lqm.borrow_rate(market_params.rate_model, util) == rate, market.name
        assert float(util) == pytest.approx(api["utilization"], rel=1e-12, abs=1e-15)
        assert lqm.qtoken_rate(state, market_params.income_ratio) == Fraction(
            *state.qtoken_rate,
        ), market.name
        assert float(Fraction(*state.qtoken_rate)) == pytest.approx(
            api["exchangeRate"],
            rel=1e-12,
        ), market.name
        liquidity = round(api["liquidity"] * 10**market.decimals)
        assert state.cash == liquidity, market.name
        borrow_apy = lqm.borrow_apy(rate, market_params.interest_period_ms)
        assert abs(float(borrow_apy) - api["borrowAPR"]) <= 1e-10, market.name
        supply_apy = float(
            lqm.supply_apy(
                rate,
                util,
                market_params.income_ratio,
                market_params.interest_period_ms,
            ),
        )
        if market.underlying == "lovelace":
            # The API adds an off-chain staking yield on idle Ada: (1 - util) * term.
            staking_terms.append((api["supplyAPR"] - supply_apy) / (1 - float(util)))
        else:
            assert abs(supply_apy - api["supplyAPR"]) <= 1e-10, market.name
            exact_supply += 1
    assert exact_supply == 32
    assert len(staking_terms) == 3
    assert all(term > 0 for term in staking_terms)
    assert max(staking_terms) - min(staking_terms) <= 1e-9


def test_borrow_headroom_is_zero_on_djed_at_the_snapshot(states, params):
    djed = _market_named("DJED")
    state = states[djed.market_id]
    cap = params[djed.market_id].borrow_cap
    assert cap == (7, 8)
    assert lqm.borrow_headroom(state.cash, state.principal, Fraction(*cap)) == 0


def test_min_interest_at_borrow_on_opened_loans(params):
    # p[15] of the live Ada MarketParams; the same 1/1250 opened every loan below.
    min_rate = Fraction(*params[_market_named("Ada").market_id].min_interest_rate)
    assert min_rate == Fraction(1, 1250)
    # The first Ada loan ever opened: principal 101 ADA, min interest 80,800.
    assert lqm.min_interest_at_borrow(101_000_000, min_rate) == 80_800
    # A 2026 NewLoan borrow: principal 2,460.081580, min interest 1,968,066.
    assert lqm.min_interest_at_borrow(2_460_081_580, min_rate) == 1_968_066


# --------------------------------------------------------------- settled transactions

# Borrow caps admit exactly the principal-only headroom of the last batched
# MarketState. (available s0, principal s3, interest s4) of that MarketState, its
# p[32] cap, and the principal the borrows against it added (s3 of the next batched
# MarketState minus s3):
#   STRIKE-ADA eb33857a0a1c3b6c6550907e77bffcaa0c5f3ec0964b908554f81e08cb2fa4fe#1,
#     next 2db344284b677018ef1844d2936748489ec8bad6b5a12e7717addc27b37a79c5#1
#     (2 borrows of 1,336.847177 and 1,336.847176 ADA from one action UTxO);
#   USDM 71e773d47b39512b20d56964d61974b0bcc89b545b83338cc69b7cb6a7f4af14#1,
#     next a09eda6adb936eb8611c65212a80253f1a398dc002f95b66adddfdbf41c8befe#3
#     (4 borrows of 49,494.4 USDM).
MAXIMAL_BORROWS = [
    pytest.param(
        2_886_294_358,
        1_364_789_706,
        1_055_034,
        Fraction(19, 20),
        4_038_484_059 - 1_364_789_706,
        2_673_740_154,
        id="strike-ada",
    ),
    pytest.param(
        312_305_599_482,
        830_743_600_359,
        2_729_826_059,
        Fraction(9, 10),
        1_028_721_200_359 - 830_743_600_359,
        198_000_679_497,
        id="usdm",
    ),
]


@pytest.mark.parametrize(
    ("available", "principal", "interest", "cap", "borrowed", "headroom"),
    MAXIMAL_BORROWS,
)
def test_borrow_headroom_admits_the_maximal_borrows(
    available,
    principal,
    interest,
    cap,
    borrowed,
    headroom,
):
    got = lqm.borrow_headroom(available, principal, cap)
    assert got == headroom
    # The borrows land just below the principal-only headroom ...
    assert 0 <= got - borrowed <= got // 5_000
    # ... and above the interest-inclusive one, which the cap therefore is not.
    owed = principal + interest
    with_interest = (cap * (available + owed) - owed) // 1
    assert with_interest < borrowed


# References of the USDC partial liquidation 17a4aabe...: the USDC MarketState
# 1d8b9c28ebc36a5e23344fce45d754acc458ecb47a67a3ab284dba6c74e7317c#1 (s5), the USDC
# MarketParams 3d72357b90273bf4c52782e140efc4bbcf0d21a4c760ae290550e0d27e8893d4#1
# (p[16]), the qEURC CollateralParams
# b0deb5f372bfcffe8bd0710b3bb7e3228d4d097c8382ca52e4e7d09af9562b5b#17 (field 2), the
# EURC MarketState 6c88e5b0bf8732dcbe42b31a495370bc23e297b86ff3700689675f9a5439b895#1
# (s9), the EURC feed
# fa317b57c3b4121c3ec81b6dac6c8c2133419699ae435fad3986603f50c3b3c1#11 and the USDC peg
# 6ef07f130bbe20d0cdf5a113a202d0e2fd915197cf52ca2431d7ab2f315ae370#12 (both in USD per
# 1e6 base units).
LIQUIDATION_TX = "17a4aabef5ee2bc954b6612496cdef0a7c1393530f660a1e0aa872797abd993f"
USDC_MARKET_INDEX = 35_930_927_316_901_916
USDC_CLOSE_FACTOR = Fraction(1, 2)
QEURC_REPAID_SHARE = Fraction(9, 10)
QEURC = "85fa65407b5321fa0e2ef9a3ec98e12a00c35871d7a620be3132003c"
QEURC_RATE = Fraction(117_344_965, 5_750_000_068)
EURC_USD = Fraction(11_193, 10_000)
USDC_USD = Fraction(1, 100)


def _loans_in(
    records: list[dict[str, Any]],
) -> list[tuple[str, LoanDatum, dict[str, int]]]:
    found = []
    for rec in records:
        parsed = parse_utxo(record_info(rec), DEPLOYMENT)
        if parsed is not None and parsed.kind is LiqwidKind.LOAN:
            found.append((rec["out_ref"], parsed.datum, dict(rec["assets"])))
    return found


def test_partial_liquidation_repays_the_close_factor_and_seizes_at_the_discount():
    spend = spend_of("partial_liquidation")
    spent = _loans_in(spend["spent"])
    created = _loans_in(spend["created"])
    assert len(spent) == 1
    assert len(created) == 1
    old_ref, old, old_assets = spent[0]
    new_ref, new, new_assets = created[0]
    assert new_ref.split("#")[0] == LIQUIDATION_TX
    assert old_ref != new_ref
    assert (old.principal, old.interest, old.min_interest, old.index) == (
        4_596_095_801,
        0,
        3_676_877,
        32_859_145_723_760_889,
    )
    debt = lqm.liquidation_debt(old, USDC_MARKET_INDEX)
    assert debt == 5_025_754_034
    repay = lqm.liquidation_repay(old, USDC_MARKET_INDEX, USDC_CLOSE_FACTOR)
    assert repay == 2_512_877_017
    # The successor keeps the unrepaid debt as principal; interest and min interest
    # reset.
    assert new.owner == old.owner
    assert (new.principal, new.interest, new.min_interest) == (debt - repay, 0, 0)
    seized = lqm.seized_value(repay, QEURC_REPAID_SHARE)
    assert seized == Fraction(25_128_770_170, 9)
    seized_qtokens = old_assets[QEURC] - new_assets[QEURC]
    assert seized_qtokens == 1_222_321_951
    assert seized * USDC_USD / (QEURC_RATE * EURC_USD) // 1 == seized_qtokens


def test_required_collateral_reaches_the_borrow_health_floor(prices):
    """At the returned qTokens HF >= 1/p[12]; one base unit fewer is below it.

    HF at borrow is sum(q * price * w) / debt and Liqwid's minimum is 1/p[12], so
    with max_ltv = p[12] * w the floor is w / max_ltv.
    """
    context = live_context()
    markets = []
    for rec in records_of("market_state", live=True):
        record = parse_utxo(record_info(rec), DEPLOYMENT)
        if record is not None:
            markets.append(market_to_view(record, context, DEPLOYMENT))
    book = PriceMap(prices={**prices.prices, **receipt_prices(markets, prices).prices})

    def health(market, terms, quantity, debt):
        position = LendingPosition(
            protocol=market.protocol,
            position_id="00" * 32 + "#0",
            origin=Origin("market", market.market_id),
            borrower=Party(PartyKind.KEY, OWNER),
            lender=None,
            borrow_unit=market.borrow_unit,
            principal=debt,
            installments_paid=0,
            collateral=(CollateralHolding(terms.unit, quantity, terms.weight),),
            rate_model=market.rate_model,
            liquidation_ltv=Fraction(1),
            on_default=DefaultRule.PRICE_LIQUIDATION,
            opened_ms=0,
        )
        (value,) = position.weighted_collateral(book)
        return value / debt

    checked = 0
    for market in markets:
        debt = 1_000 * 10 ** DEPLOYMENT.market(market.market_id).decimals
        for terms in market.collateral:
            if terms.max_ltv == 0:
                assert market.required_collateral(terms.unit, debt, book) is None
                continue
            quantity = market.required_collateral(terms.unit, debt, book)
            if quantity is None:  # collateral without a live price
                continue
            floor = terms.weight / terms.max_ltv
            assert health(market, terms, quantity, debt) >= floor
            assert health(market, terms, quantity - 1, debt) < floor
            checked += 1
    assert checked
