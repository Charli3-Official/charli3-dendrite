"""Liqwid V2 variable rate model: payoff, liquidation debt, projection, rebind (offline)."""

import json
import math
from fractions import Fraction

import pytest

from charli3_dendrite.lending import rates
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidPerpetualRate
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.math import apy_fraction
from charli3_dendrite.lending.liqwid.math import borrow_apy
from charli3_dendrite.lending.liqwid.math import liquidation_debt
from charli3_dendrite.lending.liqwid.math import payoff
from charli3_dendrite.lending.liqwid.rates import PROTOCOL_NAME
from charli3_dendrite.lending.liqwid.rates import RATE_MODELS
from charli3_dendrite.lending.liqwid.rates import LiqwidVariableRate
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.rates import MS_PER_HOUR
from charli3_dendrite.lending.rates import RateKind
from charli3_dendrite.lending.rates import rate_model_from_dict
from charli3_dendrite.lending.rates import register_rate_model
from tests.lending.liqwid.records import API
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.views import SimpleRate

PERIOD_MS = 2_000_000  # the interest period every Liqwid V2 market uses
NOW_MS = 1_790_000_000_000
_DAY_MS = 24 * MS_PER_HOUR
AMOUNTS = (0, 1, 1_249, 1_250, 1_251, 1_000_000, 5_000_000_000_000)
DURATIONS = (0, 1, PERIOD_MS, 30 * _DAY_MS, 365 * _DAY_MS)


def _market(**overrides):
    """A market's own model: bound to a state, no loan fields."""
    values = {
        "rate_num": 1,
        "rate_den": 1_000_000,
        "period_ms": PERIOD_MS,
        "market_index": 1_100_000_000_000,
    }
    values.update(overrides)
    return LiqwidVariableRate(**values)


def _loan(**overrides):
    """A loan opened at index 1.0e12, now at 1.1e12, with capitalised interest."""
    values = {
        "loan_index": 1_000_000_000_000,
        "interest": 5_000,
        "min_interest": 800,
    }
    values.update(overrides)
    return _market(**values)


def _owed(model, principal, now_ms=NOW_MS, installments_paid=0):
    return model.outstanding(
        principal,
        opened_ms=0,
        now_ms=now_ms,
        installments_paid=installments_paid,
    )


def _unfloored(model, principal, now_ms=NOW_MS, installments_paid=0):
    return model.liquidation_debt(
        principal,
        opened_ms=0,
        now_ms=now_ms,
        installments_paid=installments_paid,
    )


def test_the_model_is_liqwids_variable_rate():
    assert PROTOCOL_NAME == "LiqwidV2"
    assert LiqwidVariableRate.protocol == PROTOCOL_NAME
    assert LiqwidVariableRate.kind is RateKind.VARIABLE
    assert RATE_MODELS == (LiqwidVariableRate,)


def test_params_are_ints_and_the_record_round_trips(monkeypatch):
    monkeypatch.setattr(rates, "_REGISTRY", {})
    monkeypatch.setattr(rates, "_BUILTINS_LOADED", False)
    register_rate_model(LiqwidVariableRate)
    model = _loan(min_rate_num=3, min_rate_den=2_000)
    record = model.to_dict()
    assert record == {
        "protocol": "LiqwidV2",
        "kind": "variable",
        "params": {
            "rate_num": 1,
            "rate_den": 1_000_000,
            "period_ms": PERIOD_MS,
            "market_index": 1_100_000_000_000,
            "loan_index": 1_000_000_000_000,
            "interest": 5_000,
            "min_interest": 800,
            "min_rate_num": 3,
            "min_rate_den": 2_000,
        },
    }
    assert all(type(value) is int for value in record["params"].values())
    assert rate_model_from_dict(json.loads(json.dumps(record))) == model


def test_outstanding_is_the_floored_payoff():
    loan = _loan()
    assert _owed(loan, 1_000_000) == 1_105_500
    assert _owed(loan, 1_000_000) == payoff(
        LoanDatum(
            owner="",
            principal=1_000_000,
            interest=5_000,
            min_interest=800,
            index=1_000_000_000_000,
        ),
        1_100_000_000_000,
    )
    rounded = _market(market_index=4, loan_index=3)
    assert _owed(rounded, 10) == 14
    assert _unfloored(rounded, 10) == 14


def test_the_min_interest_floor_binds_only_the_payoff():
    flat = _loan(market_index=1_000_000_000_000, interest=0)
    assert _unfloored(flat, 1_000_000) == 1_000_000
    assert _owed(flat, 1_000_000) == 1_000_800
    assert _unfloored(flat, 1_000_000) == liquidation_debt(
        LoanDatum(
            owner="",
            principal=1_000_000,
            interest=0,
            min_interest=800,
            index=1_000_000_000_000,
        ),
        1_000_000_000_000,
    )


def test_now_ms_and_installments_do_not_move_the_debt():
    loan = _loan()
    later = NOW_MS + 365 * _DAY_MS
    assert _owed(loan, 1_000_000) == _owed(loan, 1_000_000, later, 7)
    assert _unfloored(loan, 1_000_000) == _unfloored(loan, 1_000_000, later, 7)


def test_a_zero_loan_index_owes_the_principal():
    market = _market(interest=5_000, min_interest=800)
    assert market.loan_index == 0
    assert _owed(market, 1_000_000) == 1_000_000
    assert _unfloored(market, 1_000_000) == 1_000_000


def test_the_whole_principal_remains_and_nothing_is_scheduled():
    loan = _loan()
    assert loan.remaining_principal(1_000_000, 3) == 1_000_000
    assert loan.is_late(opened_ms=0, now_ms=NOW_MS, installments_paid=0) is False
    assert loan.term_hours() is None
    assert loan.installment_period_hours() is None
    assert loan.grace_hours() == 0


def test_the_headline_rate_is_the_compounded_borrow_apy():
    model = _market()
    headline = model.headline_rate()
    assert isinstance(headline, Fraction)
    assert headline == apy_fraction(borrow_apy(Fraction(1, 1_000_000), PERIOD_MS))
    assert abs(float(headline) - math.expm1(15_768 * math.log1p(1e-6))) < 1e-14
    assert _market(rate_num=0).headline_rate() == 0


@pytest.mark.parametrize("amount", AMOUNTS)
@pytest.mark.parametrize("duration", DURATIONS)
def test_interest_for_is_never_below_the_min_interest(amount, duration):
    interest = _market().interest_for(amount, duration)
    assert interest >= -(-amount // 1250)
    assert interest >= 0


def test_interest_for_compounds_whole_periods_at_todays_rate():
    model = _market(rate_num=1, rate_den=1_000)
    assert model.interest_for(10**12, PERIOD_MS) in (10**9, 10**9 + 1)
    assert model.interest_for(10**12, 2 * PERIOD_MS) in (
        2_001_000_000,
        2_001_000_001,
    )


def test_interest_for_grows_with_the_duration():
    model = _market()
    projected = [model.interest_for(5_000_000_000_000, d) for d in DURATIONS]
    assert projected == sorted(projected)
    assert projected[-1] > projected[0]


def test_interest_for_uses_the_bound_min_rate():
    model = _market(min_rate_num=1, min_rate_den=100)
    assert model.interest_for(1_000_000, 0) == 10_000
    assert model.interest_for(1_000_001, 0) == 10_001
    assert model.interest_for(0, 365 * _DAY_MS) == 0


def test_interest_for_refuses_a_negative_duration():
    with pytest.raises(ValueError, match="duration_ms"):
        _market().interest_for(1_000_000, -1)


def test_rebind_takes_the_markets_rate_and_index_and_keeps_the_loan():
    loan = _loan()
    market = _market(
        rate_num=7,
        rate_den=3_000_000,
        period_ms=1_000_000,
        market_index=1_200_000_000_000,
        min_rate_den=999,
    )
    rebound = loan.rebind(market)
    assert rebound == LiqwidVariableRate(
        rate_num=7,
        rate_den=3_000_000,
        period_ms=1_000_000,
        market_index=1_200_000_000_000,
        loan_index=1_000_000_000_000,
        interest=5_000,
        min_interest=800,
        min_rate_num=1,
        min_rate_den=1250,
    )
    assert _owed(rebound, 1_000_000) == 1_206_000
    assert loan.market_index == 1_100_000_000_000
    assert market.rebind(market) == market


@pytest.mark.parametrize(
    "other",
    [
        SimpleRate(700),
        FluidPerpetualRate(
            interest_rate=743,
            total_installments=0,
            installment_period=0,
            initial_grace_period=0,
            repayment_time_window=0,
            penalty_fee_for_late_repayment=0,
            apy_coef=28,
        ),
    ],
)
def test_rebind_refuses_a_model_from_another_protocol(other):
    with pytest.raises(TypeError, match="LiqwidVariableRate"):
        _loan().rebind(other)


@pytest.mark.parametrize(
    "overrides",
    [
        {"rate_den": 0},
        {"min_rate_den": 0},
        {"period_ms": 0},
        {"rate_num": -1},
        {"min_rate_num": -1},
        {"market_index": -1},
        {"loan_index": -1},
    ],
)
def test_terms_the_formulas_cannot_evaluate_are_refused(overrides):
    with pytest.raises(LendingParseError):
        _loan(**overrides)


def _live(kind):
    """Every captured live record of ``kind`` that ``parse_utxo`` accepts."""
    parsed = [parse_utxo(record_info(rec)) for rec in records_of(kind.value, live=True)]
    return [record for record in parsed if record is not None and record.kind is kind]


def _market_indices():
    indices = {}
    for record in _live(LiqwidKind.MARKET_STATE):
        assert isinstance(record.datum, MarketStateDatum)
        current = indices.get(record.market_id, 0)
        indices[record.market_id] = max(current, record.datum.index)
    return indices


def test_liquidation_debt_never_exceeds_the_payoff_on_live_loans():
    indices = _market_indices()
    loans = _live(LiqwidKind.LOAN)
    assert loans
    floor_binds = 0
    for record in loans:
        loan = record.datum
        assert loan.index > 0, record.out_ref
        model = LiqwidVariableRate(
            rate_num=0,
            rate_den=1,
            period_ms=PERIOD_MS,
            market_index=indices[record.market_id],
            loan_index=loan.index,
            interest=loan.interest,
            min_interest=loan.min_interest,
        )
        owed = _owed(model, loan.principal)
        unfloored = _unfloored(model, loan.principal)
        assert unfloored <= owed, record.out_ref
        if loan.principal + loan.min_interest > unfloored:
            floor_binds += 1
            assert unfloored < owed, record.out_ref
            assert owed == loan.principal + loan.min_interest, record.out_ref
        else:
            assert unfloored == owed, record.out_ref
    assert floor_binds > 0


def test_headline_rate_matches_the_api_borrow_apr():
    deployment = default_deployment()
    states = _live(LiqwidKind.MARKET_STATE)
    assert states
    checked = set()
    for record in states:
        deployment.market(record.market_id)
        state = record.datum
        model = LiqwidVariableRate(
            rate_num=state.rate[0],
            rate_den=state.rate[1],
            period_ms=PERIOD_MS,
            market_index=state.index,
        )
        api_apr = Fraction(str(API["markets"][record.market_id]["borrowAPR"]))
        assert abs(model.headline_rate() - api_apr) <= Fraction(
            1, 10**9
        ), record.out_ref
        checked.add(record.market_id)
    assert set(API["markets"]) <= checked
