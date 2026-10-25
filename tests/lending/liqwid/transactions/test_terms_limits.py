"""Flow limits, health, collateral valuation and validity in ``terms.py``.

Synthetic MarketParams, MarketState and action records carry the live values the
limits are tested at (the Ada and USDM minimum values, the LQ supply cap of 1, the
``(0, 1)`` borrow cap and ``p[14] = 0``); the cap-scope cases use the NIGHT and DJED
MarketStates captured in ``records.json.gz``, which already sit over their caps.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.math import WeightedHolding
from charli3_dendrite.lending.liqwid.transactions import terms
from charli3_dendrite.lending.liqwid.transactions.errors import CapExceededError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    CollateralCountError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import (
    MarketAwaitingBatchError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
from charli3_dendrite.lending.liqwid.transactions.errors import MissingFeedError
from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    UnsupportedCollateralError,
)
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.utility import posix_ms_to_slot
from tests.lending.liqwid.records import parsed_records

ADA = "lovelace"
USDM = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d"
QADA = "a04ce7a52545e5e33c2867e148898d9e667a69602285f6a1298f9d68"
QDJED = "6df63e2fdde8b2c3b3396265b0cc824aa4fb999396b1c154280f6b7e"
ADA_POLICY_WIDE = "1f3dd1b685f0d87eaf92120aca3cf56da6ed91e2dbc32a95a26bce4a"
DJED = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344"
)
BATCH_MS = 1_792_252_134_000
MAX_BATCH_MS = 15_000_000
CAP_MS = BATCH_MS + MAX_BATCH_MS
NOW_DEFAULT_MS = 900_000
NOW_MARGIN_MS = 1_000

LOAN_FLOWS = (Flow.OPEN, Flow.INCREASE, Flow.REPAY, Flow.CLOSE, Flow.COLLATERAL)
MIN_VALUE_FLOWS = (Flow.SUPPLY, Flow.REDEEM, Flow.OPEN, Flow.INCREASE, Flow.REPAY)


def _raw(min_value: int, max_batch_ms: int, action_count: int) -> tuple[object, ...]:
    raw: list[object] = [0] * 36
    raw[1] = min_value
    raw[3] = max_batch_ms
    raw[7] = action_count
    return tuple(raw)


def _params(
    *,
    min_value: int = 81_000_000,
    action_count: int = 4,
    max_collaterals: int = 5,
    borrow_cap: tuple[int, int] | None = None,
    supply_cap: int | None = None,
    max_ltv: tuple[int, int] = (3, 4),
) -> MarketParamsDatum:
    return MarketParamsDatum(
        length=36,
        initial_qtoken_rate=(1, 50),
        income_ratio=(10, 9, 0, 0),
        legacy_loan_validator=None,
        interest_period_ms=2_000_000,
        rate_model=(),
        max_ltv=max_ltv,
        max_collaterals=max_collaterals,
        min_interest_rate=(1, 1250),
        close_factor=(1, 2),
        liquidation_threshold=(1, 1),
        action_validator=None,
        batch_validator=None,
        borrow_cap=borrow_cap,
        supply_cap=supply_cap,
        withdraw_zero=None,
        loan_validator=None,
        raw=_raw(min_value, MAX_BATCH_MS, action_count),
    )


def _state(*, cash: int = 40, principal: int = 120, min_ada: int = 3_000_000):
    return MarketStateDatum(
        cash=cash,
        reserve=0,
        qtokens=0,
        principal=principal,
        interest=0,
        index=1_000_000,
        rate=(1, 1_000_000),
        last_interest_ms=BATCH_MS,
        last_batch_ms=BATCH_MS,
        qtoken_rate=(1, 50),
        min_ada=min_ada,
    )


def _action(supply: int = 0, principal: int = 0) -> ActionDatum:
    return ActionDatum(supply, 0, principal, 0, 0, nonce=0)


def _action_limits(
    flow: Flow,
    *,
    params: MarketParamsDatum,
    state: MarketStateDatum,
    deltas: ActionDeltas,
    action: ActionDatum | None = None,
    action_underlying: int = 10**18,
    underlying: str = ADA,
) -> None:
    terms.check_action_limits(
        flow,
        params=params,
        state=state,
        action_datum=action or _action(),
        deltas=deltas,
        action_underlying=action_underlying,
        underlying=underlying,
    )


@pytest.fixture(scope="module")
def live() -> dict[str, tuple[MarketParamsDatum, MarketStateDatum, str]]:
    deployment = default_deployment()
    params = {
        r.market_id: r.datum for _, r in parsed_records("market_params", live=True)
    }
    states = {
        r.market_id: r.datum for _, r in parsed_records("market_state", live=True)
    }
    out = {}
    for name in ("NIGHT", "DJED"):
        market = deployment.market_by_name(name)
        out[name] = (
            params[market.market_id],
            states[market.market_id],
            market.underlying,
        )
    return out


# --- Flow -------------------------------------------------------------------------


def test_flow_values():
    assert [flow.value for flow in Flow] == [
        "supply",
        "redeem",
        "open",
        "increase",
        "repay",
        "close",
        "collateral",
    ]
    assert Flow("repay") is Flow.REPAY


# --- market limits: p[1] and p[14] -------------------------------------------------


@pytest.mark.parametrize("flow", MIN_VALUE_FLOWS)
def test_min_value_edge_on_the_ada_market(flow: Flow):
    params = _params(min_value=81_000_000)
    with pytest.raises(MinValueError) as info:
        terms.check_market_limits(
            flow,
            params=params,
            amount=80_999_999,
            output_collateral_count=1,
        )
    assert (info.value.amount, info.value.minimum) == (80_999_999, 81_000_000)
    terms.check_market_limits(
        flow,
        params=params,
        amount=81_000_000,
        output_collateral_count=1,
    )


def test_min_value_edge_on_a_usdm_repay():
    params = _params(min_value=30_000_000)
    with pytest.raises(MinValueError):
        terms.check_market_limits(
            Flow.REPAY,
            params=params,
            amount=29_999_999,
            output_collateral_count=1,
        )
    terms.check_market_limits(
        Flow.REPAY,
        params=params,
        amount=30_000_000,
        output_collateral_count=1,
    )


@pytest.mark.parametrize("flow", [Flow.CLOSE, Flow.COLLATERAL])
def test_close_and_collateral_changes_skip_the_min_value(flow: Flow):
    terms.check_market_limits(
        flow,
        params=_params(min_value=81_000_000),
        amount=1,
        output_collateral_count=1,
    )


@pytest.mark.parametrize("flow", LOAN_FLOWS)
def test_no_collateral_allowed_refuses_one_collateral(flow: Flow):
    with pytest.raises(CollateralCountError) as info:
        terms.check_market_limits(
            flow,
            params=_params(max_collaterals=0, min_value=1),
            amount=100,
            output_collateral_count=1,
        )
    assert (info.value.count, info.value.limit) == (1, 0)


@pytest.mark.parametrize("flow", LOAN_FLOWS)
def test_collateral_count_edge(flow: Flow):
    params = _params(max_collaterals=5, min_value=1)
    terms.check_market_limits(
        flow, params=params, amount=100, output_collateral_count=5
    )
    with pytest.raises(CollateralCountError):
        terms.check_market_limits(
            flow,
            params=params,
            amount=100,
            output_collateral_count=6,
        )


@pytest.mark.parametrize("flow", [Flow.SUPPLY, Flow.REDEEM])
def test_supply_and_redeem_ignore_the_collateral_count(flow: Flow):
    terms.check_market_limits(
        flow,
        params=_params(max_collaterals=0, min_value=1),
        amount=100,
        output_collateral_count=3,
    )


def test_min_value_is_checked_before_the_collateral_count():
    with pytest.raises(MinValueError):
        terms.check_market_limits(
            Flow.OPEN,
            params=_params(max_collaterals=0, min_value=81_000_000),
            amount=1,
            output_collateral_count=1,
        )


# --- action limits: caps and liquidity ---------------------------------------------


def test_a_supply_cap_of_one_refuses_any_supply():
    params = _params(supply_cap=1, min_value=1)
    with pytest.raises(CapExceededError) as info:
        _action_limits(
            Flow.SUPPLY,
            params=params,
            state=_state(cash=1_621_240_026_437, principal=0),
            deltas=ActionDeltas(supply=1, qtoken=1),
        )
    assert info.value.cap == "supply"


def test_supply_cap_edge():
    # floor(40/4) + floor(120/4) = 40; floor(203/4) = 50 leaves room for 10.
    params = _params(supply_cap=203)
    state = _state(cash=40, principal=120)
    _action_limits(
        Flow.SUPPLY,
        params=params,
        state=state,
        action=_action(supply=4),
        deltas=ActionDeltas(supply=6),
    )
    with pytest.raises(CapExceededError):
        _action_limits(
            Flow.SUPPLY,
            params=params,
            state=state,
            action=_action(supply=4),
            deltas=ActionDeltas(supply=7),
        )


def test_a_zero_borrow_cap_refuses_any_borrow():
    params = _params(borrow_cap=(0, 1))
    for flow in (Flow.OPEN, Flow.INCREASE):
        with pytest.raises(CapExceededError) as info:
            _action_limits(
                flow,
                params=params,
                state=_state(),
                deltas=ActionDeltas(principal=1),
            )
        assert info.value.cap == "borrow"


def test_borrow_cap_edge_is_inclusive():
    # (floor(120/4) + 2 + 3) / (floor(40/4) + floor(120/4)) = 35/40 = 7/8 exactly.
    params = _params(borrow_cap=(7, 8))
    state = _state(cash=40, principal=120)
    _action_limits(
        Flow.OPEN,
        params=params,
        state=state,
        action=_action(principal=2),
        deltas=ActionDeltas(principal=3),
    )
    with pytest.raises(CapExceededError):
        _action_limits(
            Flow.OPEN,
            params=params,
            state=state,
            action=_action(principal=2),
            deltas=ActionDeltas(principal=4),
        )


def test_unset_caps_are_not_checked():
    _action_limits(
        Flow.SUPPLY,
        params=_params(),
        state=_state(),
        deltas=ActionDeltas(supply=10**15),
    )
    _action_limits(
        Flow.OPEN,
        params=_params(),
        state=_state(),
        deltas=ActionDeltas(principal=10**15),
    )


def test_a_redeem_passes_on_a_market_over_its_supply_cap(live):
    params, state, underlying = live["NIGHT"]
    p7 = params.action_count
    assert params.supply_cap is not None
    assert state.cash // p7 + state.principal // p7 > params.supply_cap // p7
    _action_limits(
        Flow.REDEEM,
        params=params,
        state=state,
        deltas=ActionDeltas(supply=-500_000_000, qtoken=-9_000_000_000),
        action_underlying=1_000_000_000,
        underlying=underlying,
    )
    with pytest.raises(CapExceededError):
        _action_limits(
            Flow.SUPPLY,
            params=params,
            state=state,
            deltas=ActionDeltas(supply=500_000_000, qtoken=9_000_000_000),
            underlying=underlying,
        )


def test_a_partial_repay_passes_on_a_market_over_its_borrow_cap(live):
    params, state, underlying = live["DJED"]
    p7 = params.action_count
    assert params.borrow_cap is not None
    num, den = params.borrow_cap
    used = state.principal // p7
    assert used * den > num * (state.cash // p7 + used)
    _action_limits(
        Flow.REPAY,
        params=params,
        state=state,
        deltas=ActionDeltas(principal=-40_000_000, interest=-1_000_000),
        action_underlying=0,
        underlying=underlying,
    )
    with pytest.raises(CapExceededError):
        _action_limits(
            Flow.INCREASE,
            params=params,
            state=state,
            deltas=ActionDeltas(principal=40_000_000),
            underlying=underlying,
        )


@pytest.mark.parametrize(
    ("flow", "deltas"),
    [
        (Flow.REDEEM, ActionDeltas(supply=-100, qtoken=-5)),
        (Flow.OPEN, ActionDeltas(principal=100)),
        (Flow.INCREASE, ActionDeltas(principal=100)),
    ],
)
def test_liquidity_edge_keeps_the_ada_minimum(flow: Flow, deltas: ActionDeltas):
    state = _state(min_ada=3_000_000)
    _action_limits(
        flow,
        params=_params(),
        state=state,
        deltas=deltas,
        action_underlying=3_000_100,
    )
    with pytest.raises(InsufficientLiquidityError) as info:
        _action_limits(
            flow,
            params=_params(),
            state=state,
            deltas=deltas,
            action_underlying=3_000_099,
        )
    assert (info.value.needed, info.value.available) == (100, 99)


def test_a_token_market_pays_out_its_whole_underlying():
    _action_limits(
        Flow.OPEN,
        params=_params(),
        state=_state(min_ada=3_000_000),
        deltas=ActionDeltas(principal=100),
        action_underlying=100,
        underlying=USDM,
    )
    with pytest.raises(InsufficientLiquidityError) as info:
        _action_limits(
            Flow.OPEN,
            params=_params(),
            state=_state(min_ada=3_000_000),
            deltas=ActionDeltas(principal=101),
            action_underlying=100,
            underlying=USDM,
        )
    assert (info.value.needed, info.value.available) == (101, 100)


def test_an_action_utxo_below_the_ada_minimum_can_pay_nothing():
    with pytest.raises(InsufficientLiquidityError) as info:
        _action_limits(
            Flow.OPEN,
            params=_params(),
            state=_state(min_ada=3_000_000),
            deltas=ActionDeltas(principal=1),
            action_underlying=2_000_000,
        )
    assert (info.value.needed, info.value.available) == (1, 0)


@pytest.mark.parametrize("flow", [Flow.SUPPLY, Flow.REPAY, Flow.CLOSE])
def test_inflows_need_no_liquidity(flow: Flow):
    deltas = {
        Flow.SUPPLY: ActionDeltas(supply=100, qtoken=5),
        Flow.REPAY: ActionDeltas(principal=-100),
        Flow.CLOSE: ActionDeltas(principal=-100, interest=-3, min_interest_paid=2),
    }[flow]
    _action_limits(
        flow, params=_params(), state=_state(), deltas=deltas, action_underlying=0
    )


def test_caps_are_checked_before_liquidity():
    with pytest.raises(CapExceededError):
        _action_limits(
            Flow.OPEN,
            params=_params(borrow_cap=(0, 1)),
            state=_state(),
            deltas=ActionDeltas(principal=100),
            action_underlying=0,
        )


def test_check_limits_runs_market_limits_first():
    common: dict[str, Any] = {
        "params": _params(max_collaterals=0, borrow_cap=(0, 1), min_value=1),
        "state": _state(),
        "action_datum": _action(),
        "deltas": ActionDeltas(principal=100),
        "amount": 100,
        "action_underlying": 0,
        "underlying": ADA,
    }
    with pytest.raises(CollateralCountError):
        terms.check_limits(Flow.OPEN, output_collateral_count=1, **common)
    with pytest.raises(CapExceededError):
        terms.check_limits(Flow.OPEN, output_collateral_count=0, **common)
    common["params"] = _params(min_value=1)
    with pytest.raises(InsufficientLiquidityError):
        terms.check_limits(Flow.OPEN, output_collateral_count=0, **common)
    common["action_underlying"] = 3_000_100
    terms.check_limits(Flow.OPEN, output_collateral_count=0, **common)


# --- health -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("borrow", "repay", "collateral_in", "collateral_out", "expected"),
    [
        # borrow (open, borrow more)
        (100, 0, {}, {QADA: 10}, True),
        (100, 0, {QADA: 10}, {QADA: 10}, True),
        # remove collateral
        (0, 0, {QADA: 10}, {QADA: 9}, True),
        (0, 0, {QADA: 10, QDJED: 5}, {QADA: 10}, True),
        # add-only collateral change
        (0, 0, {QADA: 10}, {QADA: 11}, True),
        (0, 0, {QADA: 10}, {QADA: 10, QDJED: 1}, True),
        # repay with unchanged collateral
        (0, 50, {QADA: 10}, {QADA: 10}, False),
        # repay plus added collateral
        (0, 50, {QADA: 10}, {QADA: 12, QDJED: 1}, False),
        # repay while removing collateral
        (0, 50, {QADA: 10}, {QADA: 9}, True),
        # nothing changes
        (0, 0, {QADA: 10}, {QADA: 10}, False),
        # a zero quantity is a missing unit
        (0, 0, {QADA: 10}, {QADA: 10, QDJED: 0}, False),
    ],
)
def test_health_required_truth_table(
    borrow: int,
    repay: int,
    collateral_in: dict[str, int],
    collateral_out: dict[str, int],
    expected: bool,
):
    assert (
        terms.health_required(borrow, repay, collateral_in, collateral_out) is expected
    )


def _holding(price: Fraction) -> WeightedHolding:
    return WeightedHolding(
        amount=47_363_427_543,
        rate=Fraction(53_961_680_541_062, 2_555_801_649_501_385),
        price=price,
        weight=Fraction(81, 100),
    )


def test_check_health_passes_at_exact_equality_and_fails_one_unit_over():
    debt = 6_500_325_441
    borrow_price = Fraction(52_213, 1_000_000)
    max_ltv = Fraction(3, 4)
    unit = _holding(Fraction(1))
    price = debt * borrow_price / (max_ltv * unit.amount * unit.rate * unit.weight)
    holdings = [_holding(price)]
    terms.check_health(holdings, debt, borrow_price, max_ltv)
    with pytest.raises(UnhealthyLoanError) as info:
        terms.check_health(holdings, debt + 1, borrow_price, max_ltv)
    assert info.value.required == (debt + 1) * borrow_price
    assert info.value.available == debt * borrow_price
    assert info.value.required > info.value.available


@pytest.mark.parametrize(
    ("borrow_price", "max_ltv"),
    [(Fraction(0), Fraction(3, 4)), (Fraction(1), Fraction(0))],
)
def test_check_health_refuses_a_non_positive_price_or_ltv(borrow_price, max_ltv):
    with pytest.raises(ValueError, match="non-positive"):
        terms.check_health([_holding(Fraction(1))], 1, borrow_price, max_ltv)


def test_check_health_sums_every_holding():
    borrow_price = Fraction(1)
    max_ltv = Fraction(1, 2)
    holdings = [
        WeightedHolding(10, Fraction(1), Fraction(1), Fraction(1)),
        WeightedHolding(10, Fraction(2), Fraction(1), Fraction(1, 2)),
    ]
    terms.check_health(holdings, 10, borrow_price, max_ltv)
    with pytest.raises(UnhealthyLoanError):
        terms.check_health(holdings, 11, borrow_price, max_ltv)


@pytest.mark.parametrize("debt", [0, -5])
def test_check_health_passes_without_debt(debt: int):
    terms.check_health([], debt, Fraction(1), Fraction(3, 4))


def test_check_health_without_collateral_fails_any_debt():
    with pytest.raises(UnhealthyLoanError) as info:
        terms.check_health([], 1, Fraction(1), Fraction(3, 4))
    assert info.value.available == 0


# --- collateral valuation ------------------------------------------------------------


def _cparams(
    collateral: str = QADA,
    *,
    policy_wide: bool = False,
    underlying: str = ADA,
    weight: tuple[int, int] = (81, 100),
) -> CollateralParamsDatum:
    return CollateralParamsDatum(
        collateral=collateral,
        policy_wide=policy_wide,
        weight=weight,
        liquidation_discount=(9, 10),
        treasury_split=(1, 2),
        underlying=underlying,
        collateral_market_state="ab" * 28,
    )


def test_collateral_holdings_values_each_unit():
    holdings = terms.collateral_holdings(
        {QADA: 47_363_427_543, QDJED: 1_000},
        cparams={
            QADA: _cparams(),
            QDJED: _cparams(QDJED, underlying=DJED, weight=(1, 2)),
        },
        rates={
            QADA: (53_961_680_541_062, 2_555_801_649_501_385),
            QDJED: (3, 2),
        },
        prices={ADA: (41_037, 100_000), DJED: (1, 1)},
    )
    assert holdings == [
        WeightedHolding(
            amount=47_363_427_543,
            rate=Fraction(53_961_680_541_062, 2_555_801_649_501_385),
            price=Fraction(41_037, 100_000),
            weight=Fraction(81, 100),
        ),
        WeightedHolding(
            amount=1_000,
            rate=Fraction(3, 2),
            price=Fraction(1),
            weight=Fraction(1, 2),
        ),
    ]


def test_collateral_holdings_refuses_the_policy_wide_ada_record():
    record = _cparams(ADA_POLICY_WIDE, policy_wide=True, underlying="")
    with pytest.raises(UnsupportedCollateralError) as info:
        terms.collateral_holdings(
            {ADA_POLICY_WIDE + "4c50": 10},
            cparams={ADA_POLICY_WIDE + "4c50": record},
            rates={ADA_POLICY_WIDE + "4c50": (1, 1)},
            prices={ADA: (1, 1)},
        )
    assert info.value.unit == ADA_POLICY_WIDE + "4c50"


def test_collateral_holdings_refuses_a_unit_without_collateral_params():
    with pytest.raises(UnsupportedCollateralError) as info:
        terms.collateral_holdings(
            {QADA: 10, QDJED: 5},
            cparams={QADA: _cparams()},
            rates={QADA: (1, 1), QDJED: (1, 1)},
            prices={ADA: (1, 1), DJED: (1, 1)},
        )
    assert info.value.unit == QDJED


def test_collateral_holdings_refuses_a_record_without_a_collateral_market():
    with pytest.raises(UnsupportedCollateralError):
        terms.collateral_holdings(
            {QADA: 10},
            cparams={QADA: _cparams(underlying="")},
            rates={QADA: (1, 1)},
            prices={ADA: (1, 1)},
        )


def test_collateral_holdings_needs_a_price_for_each_underlying():
    with pytest.raises(MissingFeedError) as info:
        terms.collateral_holdings(
            {QADA: 10, QDJED: 5},
            cparams={QADA: _cparams(), QDJED: _cparams(QDJED, underlying=DJED)},
            rates={QADA: (1, 1), QDJED: (1, 1)},
            prices={ADA: (1, 1)},
        )
    assert info.value.asset == DJED


def test_collateral_holdings_needs_a_rate_for_each_unit():
    with pytest.raises(ValueError, match="rate"):
        terms.collateral_holdings(
            {QADA: 10},
            cparams={QADA: _cparams()},
            rates={},
            prices={ADA: (1, 1)},
        )


def test_collateral_holdings_skips_zero_quantities():
    assert (
        terms.collateral_holdings(
            {QDJED: 0},
            cparams={},
            rates={},
            prices={},
        )
        == []
    )


# --- validity -----------------------------------------------------------------------


def test_ttl_cap_and_default():
    state, params = _state(), _params()
    assert terms.ttl_cap_ms(state, params) == CAP_MS
    now = CAP_MS - 3_600_000
    assert terms.default_valid_to_ms(now, state, params) == now + NOW_DEFAULT_MS
    now = CAP_MS - 900_000
    assert terms.default_valid_to_ms(now, state, params) == CAP_MS - NOW_MARGIN_MS


def _valid_to(now_ms: int, requested_ms: int | None = None) -> int:
    return terms.valid_to_ms(
        now_ms,
        requested_ms,
        state=_state(),
        params=_params(),
        market="Ada",
    )


@pytest.mark.parametrize("now_ms", [CAP_MS, CAP_MS - 500, CAP_MS + 60_000])
def test_valid_to_refuses_a_market_awaiting_its_batch(now_ms: int):
    with pytest.raises(MarketAwaitingBatchError) as info:
        _valid_to(now_ms)
    assert (info.value.market, info.value.deadline_ms) == ("Ada", CAP_MS)


def test_valid_to_defaults():
    assert _valid_to(CAP_MS - 900_000) == CAP_MS - NOW_MARGIN_MS
    assert _valid_to(CAP_MS - 3_600_000) == CAP_MS - 3_600_000 + NOW_DEFAULT_MS


def test_valid_to_caps_a_requested_window_at_the_deadline():
    now = CAP_MS - 3_600_000
    assert _valid_to(now, CAP_MS + 1) == CAP_MS
    assert _valid_to(now, CAP_MS + 3_600_000) == CAP_MS
    assert _valid_to(now, CAP_MS) == CAP_MS
    assert _valid_to(now, now + 60_000) == now + 60_000


def test_valid_to_refuses_a_requested_window_ending_in_the_current_slot():
    now = CAP_MS - 3_600_000
    with pytest.raises(ValueError, match="requested upper bound"):
        _valid_to(now, now)
    assert posix_ms_to_slot(_valid_to(now, now + 1_000)) > posix_ms_to_slot(now)


@pytest.mark.parametrize("requested_ms", [0, CAP_MS - 7_200_000, CAP_MS - 3_600_999])
def test_valid_to_refuses_a_past_requested_bound_as_a_caller_error(requested_ms: int):
    # Hours before the deadline the market accepts a later bound, so a bound that
    # is already past is the caller's error, not a market awaiting its batch.
    with pytest.raises(ValueError, match="requested upper bound") as info:
        _valid_to(CAP_MS - 3_600_000, requested_ms)
    assert not isinstance(info.value, MarketAwaitingBatchError)


@pytest.mark.parametrize(
    ("now_ms", "requested_ms"),
    [
        (CAP_MS - 1_000, CAP_MS - 1_000),
        (CAP_MS - 500, CAP_MS - 2_000),
        (CAP_MS, CAP_MS - 60_000),
        (CAP_MS + 60_000, CAP_MS + 120_000),
    ],
)
def test_valid_to_refuses_a_market_at_its_deadline_whatever_the_request(
    now_ms: int,
    requested_ms: int,
):
    # At or within the last second before the deadline, any request is refused
    # because the market has to batch first.
    with pytest.raises(MarketAwaitingBatchError) as info:
        _valid_to(now_ms, requested_ms)
    assert (info.value.market, info.value.deadline_ms) == ("Ada", CAP_MS)


def test_valid_to_in_the_last_slots_before_the_deadline():
    # The default ends a second before the deadline, so it must still land after
    # the current slot; inside the last second it would not.
    assert _valid_to(CAP_MS - 2_000) == CAP_MS - NOW_MARGIN_MS
    with pytest.raises(MarketAwaitingBatchError):
        _valid_to(CAP_MS - 1_000)
    assert _valid_to(CAP_MS - 1_000, CAP_MS) == CAP_MS


@pytest.mark.parametrize("offset_ms", [0, 1, 999, 1_000, 1_001, 59_999])
def test_the_slot_test_matches_the_mainnet_slot_clock(offset_ms: int):
    # valid_to_ms refuses a requested bound in the current slot, by
    # posix_ms_to_slot.
    now = CAP_MS - 3_600_000 + 250
    bound = now + offset_ms
    later = posix_ms_to_slot(bound) > posix_ms_to_slot(now)
    if later:
        assert _valid_to(now, bound) == bound
    else:
        with pytest.raises(ValueError, match="requested upper bound"):
            _valid_to(now, bound)
