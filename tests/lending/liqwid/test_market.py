"""Liqwid V2 market states and loans as common lending views (offline)."""

import math
from collections import Counter
from dataclasses import replace
from fractions import Fraction

import cbor2
import pytest

from charli3_dendrite.lending.liqwid import market as liqwid_market
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.lifecycle import LiqwidLifecycle
from charli3_dendrite.lending.liqwid.lifecycle import collateral_units
from charli3_dendrite.lending.liqwid.loader import snapshot
from charli3_dendrite.lending.liqwid.market import LiqwidV2MarketSource
from charli3_dendrite.lending.liqwid.market import collateral_terms
from charli3_dendrite.lending.liqwid.market import loan_to_position
from charli3_dendrite.lending.liqwid.market import market_parameters
from charli3_dendrite.lending.liqwid.market import market_to_view
from charli3_dendrite.lending.liqwid.math import liquidation_weight
from charli3_dendrite.lending.liqwid.rates import PROTOCOL_NAME
from charli3_dendrite.lending.liqwid.rates import LiqwidVariableRate
from charli3_dendrite.lending.markets import CONTEXT_ROLE
from charli3_dendrite.lending.markets import VIEW_ROLE
from charli3_dendrite.lending.markets import ParseContext
from charli3_dendrite.lending.markets import PositionLifecycle
from charli3_dendrite.lending.markets import WatchSpec
from charli3_dendrite.lending.math import apportion
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.normalized import Origin
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.rates import register_rate_model
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.liqwid.records import API
from tests.lending.liqwid.records import context_at
from tests.lending.liqwid.records import live_context
from tests.lending.liqwid.records import live_price_map
from tests.lending.liqwid.records import parsed_records
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.liqwid.test_loader import FakeBackend

# Views rebuilt from to_dict() need the Liqwid rate model registered.
register_rate_model(LiqwidVariableRate)

DEPLOYMENT = default_deployment()
NOW_MS = 1_790_000_000_000
POLICY_HEX_LEN = 56
ADA_MARKET = "5a3cb4f52fb3a00ab5abe62080618623811ccafab9c760d0b686e44c"
ADA_QTOKEN = "a04ce7a52545e5e33c2867e148898d9e667a69602285f6a1298f9d68"
GENESIS_ADA_PARAMS = (
    "36d4bfe57e1ebd90e0e586f228b84903403e72c9d6d54b88994f66a0372f18ee#1"
)
# A MarketParams output whose NFT sits on an empty constructor (C0[]).
PARKED_DATUM = "d87980"


def _without_market(context, kind, market_id):
    """``context`` minus the records of ``kind`` for ``market_id``."""
    kept = []
    for info in context.utxos:
        record = parse_utxo(info, DEPLOYMENT)
        if record is None or record.kind is not kind or record.market_id != market_id:
            kept.append(info)
    return ParseContext(context.slot, tuple(kept))


@pytest.fixture(scope="module")
def live_ctx():
    return live_context()


@pytest.fixture(scope="module")
def live_states():
    """Market id -> (fixture entry, record) of each market's live MarketState."""
    states = {}
    for rec, record in parsed_records("market_state", live=True):
        assert record.market_id not in states
        states[record.market_id] = (rec, record)
    return states


@pytest.fixture(scope="module")
def live_markets(live_ctx, live_states):
    return {
        market_id: market_to_view(record, live_ctx, DEPLOYMENT)
        for market_id, (_, record) in live_states.items()
    }


@pytest.fixture(scope="module")
def book(live_markets):
    """Liqwid's own oracle prices in lovelace, plus every qToken through its s9."""
    return live_price_map(live_markets.values())


@pytest.fixture(scope="module")
def live_loans(live_ctx):
    """(fixture entry, record, position bound by the live context) per live loan."""
    return [
        (rec, record, loan_to_position(record, live_ctx, DEPLOYMENT))
        for rec, record in parsed_records("loan", live=True)
    ]


def test_every_live_market_state_is_a_view_matching_the_api(live_states, live_markets):
    assert set(live_markets) == set(API["markets"])
    for market_id, view in live_markets.items():
        state = live_states[market_id][1].datum
        market = DEPLOYMENT.market(market_id)
        api = API["markets"][market_id]
        assert view.protocol == PROTOCOL_NAME == "LiqwidV2"
        assert view.market_id == market_id == market.market_state_policy
        assert view.borrow_unit == market.underlying
        assert view.lender is None
        assert view.on_default is DefaultRule.PRICE_LIQUIDATION
        assert view.permissioned is False
        assert view.receipt_unit == market.qtoken_policy
        assert len(view.receipt_unit) == POLICY_HEX_LEN
        assert view.available_liquidity == state.cash
        assert view.total_principal == state.principal
        assert view.receipt_rate == Fraction(*state.qtoken_rate)
        assert (view.rate_model.rate_num, view.rate_model.rate_den) == tuple(state.rate)
        assert view.rate_model.market_index == state.index
        assert view.rate_model.loan_index == 0
        assert math.isclose(
            float(view.utilization),
            float(api["utilization"]),
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
        assert math.isclose(
            view.available_liquidity / 10**market.decimals,
            float(api["liquidity"]),
            rel_tol=1e-9,
            abs_tol=10**-market.decimals,
        )
        assert math.isclose(
            float(view.receipt_rate), float(api["exchangeRate"]), rel_tol=1e-9
        )


def test_terms_follow_the_market_parameters(live_ctx, live_markets):
    for market_id, view in live_markets.items():
        params, records = market_parameters(
            DEPLOYMENT.market(market_id), live_ctx, DEPLOYMENT
        )
        cap = params.borrow_cap
        assert view.max_utilization == (None if cap is None else Fraction(*cap))
        units = [terms.unit for terms in view.collateral]
        assert units == [record.collateral for record in records]
        assert units == sorted(units)
        for terms, record in zip(view.collateral, records, strict=True):
            weight = Fraction(*record.weight)
            assert terms.weight == weight
            assert terms.policy_wide is record.policy_wide
            assert terms.max_ltv == Fraction(*params.max_ltv) * weight
            assert terms.fixed_ratio is None
            assert terms.liquidation_ltv == weight * Fraction(
                *params.liquidation_threshold
            )
            assert terms.liquidation_ltv == liquidation_weight(
                record.weight,
                params.liquidation_threshold,
            )
            assert terms.liquidation_penalty is None
            assert terms.liquidation_discount == 1 - Fraction(
                *record.liquidation_discount
            )
            assert terms.equity_to_borrower is True
            assert terms.price_source.source == OracleSource.LIQWID
            assert terms.price_source.token == record.collateral
            assert terms.price_source.extra == {
                "underlying": record.underlying,
                "market_state": record.collateral_market_state,
            }


def test_terms_weigh_health_through_liquidation_weight(live_ctx, monkeypatch):
    calls = []

    def spy(weight, threshold):
        calls.append((weight, threshold))
        return Fraction(7, 9)

    monkeypatch.setattr(liqwid_market, "liquidation_weight", spy)
    market_id = next(iter(parsed_records("market_state", live=True)))[1].market_id
    params, records = market_parameters(
        DEPLOYMENT.market(market_id), live_ctx, DEPLOYMENT
    )
    assert records
    for record in records:
        terms = collateral_terms(params, record, DEPLOYMENT)
        assert terms.liquidation_ltv == Fraction(7, 9)
        assert calls[-1] == (record.weight, params.liquidation_threshold)
    assert len(calls) == len(records)


def test_market_views_round_trip(live_markets):
    for view in live_markets.values():
        data = view.to_dict()
        assert LendingMarket.from_dict(data) == view
        assert LendingMarket.from_dict(data).to_dict() == data
        assert data["rate_model"]["protocol"] == "LiqwidV2"
        assert "receipt_rate" in data
        assert "total_principal" in data


def test_market_state_without_market_parameters_raises(live_ctx, live_states):
    _, record = live_states[ADA_MARKET]
    with pytest.raises(LendingParseError, match="no market parameters at slot"):
        market_to_view(record, ParseContext(slot=API["slot"]), DEPLOYMENT)
    # Other markets' parameters never stand in for the market's own.
    others_only = _without_market(live_ctx, LiqwidKind.MARKET_PARAMS, ADA_MARKET)
    with pytest.raises(LendingParseError, match="no market parameters at slot"):
        market_to_view(record, others_only, DEPLOYMENT)


def test_market_without_collateral_params_has_empty_collateral(live_ctx, live_markets):
    """Review Focus 2: no CollateralParams record is a valid market, not an error."""
    with_records = set()
    for info in live_ctx.utxos:
        record = parse_utxo(info, DEPLOYMENT)
        if record is not None and record.kind is LiqwidKind.COLLATERAL_PARAMS:
            with_records.add(record.market_id)
    bare = [market_id for market_id in live_markets if market_id not in with_records]
    assert bare  # LQ, POL, STRIKE and SNEK2 lend against nothing on mainnet
    for market_id in bare:
        assert live_markets[market_id].collateral == ()
        assert live_markets[market_id].to_dict()["collateral"] == []


def _live_params_entry(market_id):
    for rec, record in parsed_records("market_params", live=True):
        if record.market_id == market_id:
            return rec
    raise AssertionError(f"no live MarketParams captured for {market_id}")


def test_parked_market_params_fall_back_to_the_newest_decodable(
    live_ctx, live_states, live_markets
):
    """Review Focus 2: a newer C0[] MarketParams output is skipped, never an error."""
    entry = _live_params_entry(ADA_MARKET)
    newest = record_info(entry)
    parked = record_info(
        entry,
        tx_hash="ab" * 32,
        tx_index=0,
        block_time=newest.block_time + 1,
        datum_cbor=PARKED_DATUM,
    )
    assert parse_utxo(parked, DEPLOYMENT) is None
    _, state = live_states[ADA_MARKET]
    with_parked = ParseContext(live_ctx.slot, (*live_ctx.utxos, parked))
    assert market_to_view(state, with_parked, DEPLOYMENT) == live_markets[ADA_MARKET]
    only_parked = _without_market(live_ctx, LiqwidKind.MARKET_PARAMS, ADA_MARKET)
    only_parked = ParseContext(only_parked.slot, (*only_parked.utxos, parked))
    with pytest.raises(LendingParseError, match="no market parameters at slot"):
        market_to_view(state, only_parked, DEPLOYMENT)


def test_bare_market_with_a_newer_parked_params_output_is_still_a_view(
    live_ctx, live_states, live_markets
):
    """Review Focus 2, combined: no CollateralParams and a newer C0[] MarketParams."""
    with_records = {
        record.market_id
        for record in (parse_utxo(info, DEPLOYMENT) for info in live_ctx.utxos)
        if record is not None and record.kind is LiqwidKind.COLLATERAL_PARAMS
    }
    bare = sorted(set(live_markets) - with_records)
    assert bare
    # Newer than every live output, so the parked one is the market's newest.
    newest_time = max(info.block_time for info in live_ctx.utxos)
    for market_id in bare:
        parked = record_info(
            _live_params_entry(market_id),
            tx_hash="ac" * 32,
            tx_index=0,
            block_time=newest_time + 1,
            datum_cbor=PARKED_DATUM,
        )
        assert parse_utxo(parked, DEPLOYMENT) is None
        _, state = live_states[market_id]
        with_parked = ParseContext(live_ctx.slot, (*live_ctx.utxos, parked))
        view = market_to_view(state, with_parked, DEPLOYMENT)
        assert view == live_markets[market_id]
        assert view.collateral == ()


def test_the_newest_decodable_market_params_win(live_ctx, live_states, live_markets):
    (genesis,) = [
        rec
        for rec in records_of("market_params")
        if rec["out_ref"] == GENESIS_ADA_PARAMS
    ]
    _, state = live_states[ADA_MARKET]
    older = record_info(genesis)
    assert older.block_time < record_info(_live_params_entry(ADA_MARKET)).block_time
    with_older = ParseContext(live_ctx.slot, (*live_ctx.utxos, older))
    assert market_to_view(state, with_older, DEPLOYMENT) == live_markets[ADA_MARKET]
    # The same list27 record made the newest: its terms and its absent cap apply.
    newer = record_info(genesis, block_time=10**12)
    view = market_to_view(
        state, ParseContext(live_ctx.slot, (*live_ctx.utxos, newer)), DEPLOYMENT
    )
    assert view.max_utilization is None
    ada_terms = view.terms_for(ADA_QTOKEN)
    assert ada_terms.max_ltv == Fraction(3, 4) * ada_terms.weight
    assert ada_terms.liquidation_ltv == Fraction(4, 5) * ada_terms.weight
    assert view.rate_model.period_ms == 2_000_000


def test_genesis_ada_state_is_a_view():
    entries = [
        (rec, record)
        for rec, record in parsed_records("market_state")
        if record.market_id == ADA_MARKET
    ]
    rec, record = min(entries, key=lambda pair: pair[0]["slot"])
    assert record.datum == MarketStateDatum(
        cash=0,
        reserve=0,
        qtokens=0,
        principal=0,
        interest=0,
        index=16777216000000000,
        rate=(2827979481, 2251799813685248),
        last_interest_ms=1675302300000,
        last_batch_ms=1675302300000,
        qtoken_rate=(1, 50),
        min_ada=3000000,
    )
    # Genesis parameters were created in the same block as this state.
    view = market_to_view(record, context_at(rec["slot"] + 1), DEPLOYMENT)
    assert view.available_liquidity == 0
    assert view.total_principal == 0
    assert view.utilization == 0
    assert view.supply_rate == 0
    assert view.receipt_rate == Fraction(1, 50)
    assert view.max_utilization is None
    assert view.rate_model.market_index == 16777216000000000
    assert (view.rate_model.rate_num, view.rate_model.rate_den) == (
        2827979481,
        2251799813685248,
    )
    assert view.rate_model.period_ms == 2_000_000
    assert (view.rate_model.min_rate_num, view.rate_model.min_rate_den) == (1, 1250)
    (terms,) = view.collateral
    assert terms.unit == ADA_QTOKEN
    assert terms.weight == 1
    assert terms.max_ltv == Fraction(3, 4)
    assert terms.liquidation_ltv == Fraction(4, 5)
    assert terms.liquidation_discount == Fraction(1, 10)
    assert terms.price_source.extra == {
        "underlying": "lovelace",
        "market_state": ADA_MARKET,
    }


def test_terms_do_not_change_within_a_parameter_epoch(
    live_ctx, live_states, live_markets
):
    rec, _ = live_states[ADA_MARKET]
    fields = cbor2.loads(bytes.fromhex(rec["datum_cbor"]))
    fields[0] += 1_000_000
    fields[5] += 12_345
    fields[9] = [fields[9][0] + 1, fields[9][1]]
    later = parse_utxo(
        record_info(
            rec, tx_hash="cd" * 32, tx_index=0, datum_cbor=cbor2.dumps(fields).hex()
        ),
        DEPLOYMENT,
    )
    first = live_markets[ADA_MARKET]
    second = market_to_view(later, live_ctx, DEPLOYMENT)
    assert first.collateral
    assert second.rate_model != first.rate_model
    assert second.receipt_rate != first.receipt_rate
    assert second.available_liquidity != first.available_liquidity
    assert [t.to_dict() for t in second.collateral] == [
        t.to_dict() for t in first.collateral
    ]


def test_script_class_record_gives_policy_wide_terms(live_ctx, live_markets):
    record = next(
        record
        for _, record in parsed_records("collateral_params")
        if record.datum.policy_wide
    )
    datum = record.datum
    assert isinstance(datum, CollateralParamsDatum)
    assert len(datum.collateral) == POLICY_HEX_LEN
    params, _ = market_parameters(
        DEPLOYMENT.market(record.market_id), live_ctx, DEPLOYMENT
    )
    terms = collateral_terms(params, datum, DEPLOYMENT)
    assert terms.policy_wide is True
    assert terms.unit == datum.collateral
    assert terms.weight == Fraction(*datum.weight)
    assert terms.accepts(datum.collateral + "abcd")
    assert not terms.accepts("lovelace")
    assert terms.price_source.extra == {
        "underlying": datum.underlying,
        "market_state": datum.collateral_market_state,
    }
    # A loan holding the policy keeps the full unit and matches the policy-wide terms.
    holders = [
        record
        for _, record in parsed_records("loan")
        if any(unit[:POLICY_HEX_LEN] == datum.collateral for unit in record.info.assets)
    ]
    assert holders
    position = loan_to_position(holders[0], deployment=DEPLOYMENT)
    held = [h for h in position.collateral if h.unit.startswith(datum.collateral)]
    assert held
    assert all(h.amount == holders[0].info.assets[h.unit] for h in held)
    market = replace(live_markets[holders[0].market_id], collateral=(terms,))
    bound = position.at_market(market)
    for holding in bound.collateral:
        if holding.unit.startswith(datum.collateral):
            assert holding.liquidation_ltv == terms.liquidation_ltv


def _collateral_units(info):
    """Every unit of a loan UTxO except ADA and loan tokens, in canonical order."""
    return sorted(
        unit
        for unit in info.assets
        if unit != "lovelace"
        and unit[:POLICY_HEX_LEN] not in DEPLOYMENT.loan_token_policies
    )


def test_every_live_loan_is_a_position(live_loans, live_states):
    assert live_loans
    for _, record, position in live_loans:
        info, datum = record.info, record.datum
        market = DEPLOYMENT.market(record.market_id)
        assert position.protocol == "LiqwidV2"
        assert (
            position.position_id == record.out_ref == f"{info.tx_hash}#{info.tx_index}"
        )
        assert position.origin == Origin("market", record.market_id)
        assert position.borrower == Party(PartyKind.KEY, datum.owner)
        assert position.lender is None
        assert position.borrow_unit == market.underlying
        assert position.principal == datum.principal
        assert position.installments_paid == 0
        assert position.liquidation_ltv == Fraction(1)
        assert position.on_default is DefaultRule.PRICE_LIQUIDATION
        assert position.opened_ms == info.block_time * 1000
        assert [h.unit for h in position.collateral] == _collateral_units(info)
        assert all(h.amount == info.assets[h.unit] for h in position.collateral)
        model = position.rate_model
        assert model.market_index == live_states[record.market_id][1].datum.index
        assert (model.loan_index, model.interest, model.min_interest) == (
            datum.index,
            datum.interest,
            datum.min_interest,
        )


def test_live_qtoken_holdings_are_exact_units(live_loans, live_markets):
    """A qToken holding is the bare 56-hex policy, matched by exact terms only."""
    for _, record, position in live_loans:
        market = live_markets[record.market_id]
        for holding in position.collateral:
            assert len(holding.unit) == POLICY_HEX_LEN
            assert DEPLOYMENT.market_of_qtoken(holding.unit) is not None
            terms = market.terms_for(holding.unit)
            if terms is None:
                assert holding.liquidation_ltv == 0
                continue
            assert terms.policy_wide is False
            assert terms.accepts(holding.unit)
            assert not terms.accepts(holding.unit + "00")
            assert holding.liquidation_ltv == terms.liquidation_ltv


def test_loan_without_context_is_unbound(live_loans):
    for _, record, _ in live_loans:
        bare = loan_to_position(record, deployment=DEPLOYMENT)
        assert bare.rate_model.market_index == record.datum.index
        assert all(h.liquidation_ltv is None for h in bare.collateral)
        # A context that holds nothing of the market binds nothing.
        empty = ParseContext(slot=API["slot"])
        assert loan_to_position(record, empty, DEPLOYMENT) == bare


def test_at_market_rebinds_a_bare_loan_to_the_live_market(live_loans, live_markets):
    for _, record, bound in live_loans:
        market = live_markets[record.market_id]
        rebound = loan_to_position(record, deployment=DEPLOYMENT).at_market(market)
        assert rebound.rate_model.market_index == market.rate_model.market_index
        assert (rebound.rate_model.rate_num, rebound.rate_model.rate_den) == (
            market.rate_model.rate_num,
            market.rate_model.rate_den,
        )
        assert rebound == bound


def test_positions_round_trip(live_loans):
    for _, _, position in live_loans:
        data = position.to_dict()
        assert LendingPosition.from_dict(data) == position
        assert LendingPosition.from_dict(data).to_dict() == data


def _served(live_loans):
    """Loans with outstanding principal and at least one collateral."""
    return [p for _, _, p in live_loans if p.principal > 0 and p.collateral]


def test_debt_and_interest_split_exactly_over_weighted_collateral(live_loans, book):
    multi = [p for p in _served(live_loans) if len(p.collateral) > 1]
    assert multi
    for position in multi:
        weights = position.weighted_collateral(book)
        assert None not in weights
        assert any(weight > 0 for weight in weights)
        for total in (position.current_debt(NOW_MS), position.interest_accrued(NOW_MS)):
            parts = apportion(total, list(weights))
            assert len(parts) == len(position.collateral)
            assert sum(parts) == total
            assert min(parts) >= 0


def test_health_factor_matches_the_api(live_loans, book):
    served = _served(live_loans)
    assert served
    mismatches = []
    for position in served:
        expected = float(API["loans"][position.position_id]["healthFactor"])
        got = float(position.health_factor(book, NOW_MS))
        if not math.isclose(got, expected, rel_tol=1e-6):
            mismatches.append((position.position_id, got, expected))
    assert mismatches == []


SOURCE_NOW_MS = 1_790_000_000_000


def _live_example(kind):
    """The first captured live record ``parse_utxo`` classifies as ``kind``."""
    for rec in records_of(kind.value, live=True):
        record = parse_utxo(record_info(rec))
        if record is not None and record.kind is kind:
            return rec, record
    raise AssertionError(f"no live {kind.value} record captured")


def test_source_name_and_lifecycle():
    source = LiqwidV2MarketSource()
    assert source.name == PROTOCOL_NAME == "LiqwidV2"
    assert isinstance(source.lifecycle, LiqwidLifecycle)
    assert isinstance(source.lifecycle, PositionLifecycle)
    assert (
        source.lifecycle.loan_token_policies == default_deployment().loan_token_policies
    )


def test_selectors_hold_every_view_and_context_spec():
    deployment = default_deployment()
    specs = LiqwidV2MarketSource().selectors()
    assert specs == [s.watch_spec() for s in liqwid_selectors()]
    assert {s.role for s in specs} == {VIEW_ROLE, CONTEXT_ROLE}
    first_context = next(i for i, s in enumerate(specs) if s.role == CONTEXT_ROLE)
    assert all(s.role == VIEW_ROLE for s in specs[:first_context])
    assert all(s.role == CONTEXT_ROLE for s in specs[first_context:])

    params = deployment.param_credential
    expected_context = (
        {
            WatchSpec(params, m.market_params_policy, CONTEXT_ROLE)
            for m in deployment.markets
        }
        | {
            WatchSpec(params, m.collateral_params_policy, CONTEXT_ROLE)
            for m in deployment.markets
        }
        | {
            WatchSpec(o.script_hash, deployment.oracle_token_policy, CONTEXT_ROLE)
            for o in deployment.oracle_validators
        }
    )
    assert {s for s in specs if s.role == CONTEXT_ROLE} == expected_context

    views = [s for s in specs if s.role == VIEW_ROLE]
    for market in deployment.markets:
        state_credentials = {
            s.payment_credential for s in views if s.identity_policy == market.market_id
        }
        assert set(market.market_state_validators) <= state_credentials
        loan_credentials = {
            s.payment_credential
            for s in views
            if s.identity_policy in market.loan_token_policies
        }
        assert set(market.loan_validators) <= loan_credentials
    identities = {m.market_id for m in deployment.markets}
    assert {s.identity_policy for s in views} <= (
        identities | deployment.loan_token_policies
    )


def test_parse_turns_a_market_state_into_its_market():
    rec, record = _live_example(LiqwidKind.MARKET_STATE)
    context = live_context()
    market = LiqwidV2MarketSource().parse(record_info(rec), context=context)
    assert isinstance(market, LendingMarket)
    assert market.market_id == record.market_id
    assert market.to_dict() == market_to_view(record, context).to_dict()


def test_parse_turns_a_loan_into_its_position_with_or_without_context():
    rec, record = _live_example(LiqwidKind.LOAN)
    source = LiqwidV2MarketSource()
    bare = source.parse(record_info(rec))
    assert isinstance(bare, LendingPosition)
    assert bare.position_id == record.out_ref
    assert bare.to_dict() == loan_to_position(record).to_dict()
    context = live_context()
    stamped = source.parse(record_info(rec), context=context)
    assert isinstance(stamped, LendingPosition)
    assert stamped.to_dict() == loan_to_position(record, context).to_dict()


@pytest.mark.parametrize(
    "kind",
    [LiqwidKind.MARKET_PARAMS, LiqwidKind.COLLATERAL_PARAMS, LiqwidKind.ORACLE],
)
def test_parse_returns_none_for_a_context_record(kind):
    rec, _ = _live_example(kind)
    source = LiqwidV2MarketSource()
    assert source.parse(record_info(rec)) is None
    assert source.parse(record_info(rec), context=live_context()) is None


def test_parse_returns_none_for_a_utxo_that_is_not_genuine():
    source = LiqwidV2MarketSource()
    for kind in (LiqwidKind.MARKET_STATE, LiqwidKind.LOAN):
        rec, _ = _live_example(kind)
        elsewhere = record_info(rec, address=script_payment_address("00" * 28))
        assert source.parse(elsewhere, context=live_context()) is None
        datumless = record_info(rec, datum_cbor="")
        assert source.parse(datumless, context=live_context()) is None


def test_parse_needs_market_parameters_for_a_market_state():
    rec, _ = _live_example(LiqwidKind.MARKET_STATE)
    source = LiqwidV2MarketSource()
    with pytest.raises(LendingParseError):
        source.parse(record_info(rec))
    with pytest.raises(LendingParseError):
        source.parse(record_info(rec), context=None)
    with pytest.raises(LendingParseError):
        source.parse(record_info(rec), context=ParseContext(slot=None))


def test_load_gives_every_market_and_live_loan():
    loaded = LiqwidV2MarketSource().load(FakeBackend(), now_ms=SOURCE_NOW_MS)
    found = snapshot(FakeBackend(), SOURCE_NOW_MS)
    market_ids = [m.market_id for m in loaded.markets]
    assert len(market_ids) == len(set(market_ids)) == len(API["markets"]) == 35
    assert set(market_ids) == set(API["markets"])
    assert {m.protocol for m in loaded.markets} == {PROTOCOL_NAME}
    assert [m.to_dict() for m in loaded.markets] == [
        market_to_view(r, found.context).to_dict() for r in found.market_states
    ]
    assert loaded.positions
    assert [p.position_id for p in loaded.positions] == [r.out_ref for r in found.loans]
    assert [p.to_dict() for p in loaded.positions] == [
        loan_to_position(r, found.context).to_dict() for r in found.loans
    ]
    assert loaded.requests == ()


def test_load_leaves_out_markets_it_cannot_convert():
    without_params = {
        kind: records_of(kind, live=True)
        for kind in ("market_state", "collateral_params", "oracle", "loan")
    }
    loaded = LiqwidV2MarketSource().load(
        FakeBackend(without_params),
        now_ms=SOURCE_NOW_MS,
    )
    assert loaded.markets == ()
    found = snapshot(FakeBackend(), SOURCE_NOW_MS)
    assert [p.position_id for p in loaded.positions] == [r.out_ref for r in found.loans]


def test_load_builds_each_market_view_once(monkeypatch):
    built = Counter()
    build = liqwid_market._market_view

    def counted(state, *args, **kwargs):
        built[state.out_ref] += 1
        return build(state, *args, **kwargs)

    monkeypatch.setattr(liqwid_market, "_market_view", counted)
    loaded = LiqwidV2MarketSource().load(FakeBackend(), now_ms=SOURCE_NOW_MS)
    assert len(loaded.positions) > len(loaded.markets)
    assert sorted(built) == [r.out_ref for r in snapshot(FakeBackend()).market_states]
    assert set(built.values()) == {1}


def test_loan_binds_to_given_market_views_as_to_its_context():
    _, record = _live_example(LiqwidKind.LOAN)
    context = live_context()
    views = {
        market.market_id: market
        for market in LiqwidV2MarketSource().load(FakeBackend()).markets
    }
    bound = loan_to_position(record, context, markets=views)
    assert bound.to_dict() == loan_to_position(record, context).to_dict()
    unbound = loan_to_position(record, context, markets={})
    assert unbound.to_dict() == loan_to_position(record).to_dict()


def _raising(error):
    def build(*args, **kwargs):
        raise error

    return build


def test_a_market_state_conversion_error_names_the_market_state(monkeypatch):
    _, record = _live_example(LiqwidKind.MARKET_STATE)
    monkeypatch.setattr(liqwid_market, "utilization", _raising(ZeroDivisionError("x")))
    with pytest.raises(LendingParseError) as raised:
        market_to_view(record, live_context(), DEPLOYMENT)
    assert str(raised.value) == f"market state {record.out_ref}: x"


def test_a_market_state_parse_error_passes_through_unprefixed(monkeypatch):
    _, record = _live_example(LiqwidKind.MARKET_STATE)
    error = LendingParseError("a Liqwid rate needs a positive denominator")
    monkeypatch.setattr(liqwid_market, "LiqwidVariableRate", _raising(error))
    with pytest.raises(LendingParseError) as raised:
        market_to_view(record, live_context(), DEPLOYMENT)
    assert raised.value is error


def test_a_loan_conversion_error_names_the_loan(monkeypatch):
    _, record = _live_example(LiqwidKind.LOAN)
    monkeypatch.setattr(liqwid_market, "_holdings", _raising(KeyError("x")))
    with pytest.raises(LendingParseError) as raised:
        loan_to_position(record, None, DEPLOYMENT)
    assert str(raised.value) == f"loan {record.out_ref}: 'x'"


def test_a_loan_parse_error_passes_through_unprefixed(monkeypatch):
    _, record = _live_example(LiqwidKind.LOAN)
    error = LendingParseError("a Liqwid interest index must not be negative")
    monkeypatch.setattr(liqwid_market, "LiqwidVariableRate", _raising(error))
    with pytest.raises(LendingParseError) as raised:
        loan_to_position(record, None, DEPLOYMENT)
    assert raised.value is error


def test_loan_holdings_are_the_shared_collateral_units(live_loans):
    for _, record, bound in live_loans:
        units = collateral_units(record.info, DEPLOYMENT.loan_token_policies)
        bare = loan_to_position(record, deployment=DEPLOYMENT)
        for position in (bare, bound):
            assert [(h.unit, h.amount) for h in position.collateral] == list(units)
