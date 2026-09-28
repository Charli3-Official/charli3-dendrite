"""FluidTokens V4 pools, loans and requests as common lending views (offline)."""

from dataclasses import replace
from fractions import Fraction

import cbor2
import pytest
from pycardano import RawCBOR
from pycardano import RawPlutusData

from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_collateral_amount,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_output_lovelace,
)
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    request_principal_bounds,
)
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4 import datums as v4
from charli3_dendrite.lending.fluidtokens_v4.market import lendable_principal
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidAmortizedRate
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidFlatTermRate
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidPerpetualRate
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LoanState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4RequestState
from charli3_dendrite.lending.normalized import BorrowRequest
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.normalized import Origin
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.oracles.models import OracleSource
from tests.lending.fluidtokens_v4.records import FIX
from tests.lending.fluidtokens_v4.records import record_info
from tests.lending.fluidtokens_v4.records import v4_request_record
from tests.lending.views import prices

_FALSE = RawPlutusData(cbor2.CBORTag(121, []))
_NONE = RawPlutusData(cbor2.CBORTag(122, []))
NOW_MS = 1_790_000_000_000


def _single_option_ada_pool_index():
    """A captured ADA pool with one collateral option, liquidating at 4/5, 100 per mille."""
    for index, rec in enumerate(FIX["pool"]):
        datum = v4.PoolDatum.from_cbor(rec["datum_cbor"])
        mode = datum.common_data.liquidation_mode
        if (
            datum.common_data.principal_asset.unit() == "lovelace"
            and len(datum.collateral_options) == 1
            and (mode.l_tv, mode.l_tv_divider) == (100, 125)
            and mode.partial_liquidation_penalty_per_mille == 100
        ):
            return index
    raise AssertionError("no such captured pool")


ADA = _single_option_ada_pool_index()


def _pool(index=ADA, **datum_changes):
    rec = dict(FIX["pool"][index])
    if datum_changes:
        datum = v4.PoolDatum.from_cbor(rec["datum_cbor"])
        common = datum_changes.pop("common", {})
        if common:
            datum = replace(datum, common_data=replace(datum.common_data, **common))
        rec["datum_cbor"] = replace(datum, **datum_changes).to_cbor_hex()
    return FluidV4PoolState.from_record(record_info(rec))


def _manager_of(pool):
    for rec in FIX["pool_manager"]:
        manager = FluidV4PoolManagerState.from_record(record_info(rec))
        if manager.pool_id == pool.pool_id:
            return manager
    raise AssertionError("fixture pool without its manager")


def _loan(index=0, **datum_changes):
    rec = dict(FIX["loan"][index])
    if datum_changes:
        datum = v4.LoanDatum.from_cbor(rec["datum_cbor"])
        rec["datum_cbor"] = replace(datum, **datum_changes).to_cbor_hex()
    return FluidV4LoanState.from_record(record_info(rec))


def _token_pool_index():
    return next(
        i for i, rec in enumerate(FIX["pool"]) if _pool(i).borrowable_unit != "lovelace"
    )


def test_every_captured_pool_converts_with_its_manager():
    for index in range(len(FIX["pool"])):
        pool = _pool(index)
        market = pool.to_market(_manager_of(pool))
        assert market.protocol == "FluidTokensV4"
        assert market.market_id == c.POOL_POLICY + pool.pool_id
        assert market.borrow_unit == pool.borrowable_unit
        assert market.lender == Party(
            PartyKind.KEY,
            _manager_of(pool).owner_auth.hash_hex,
            "signature",
        )
        assert 0 <= market.available_liquidity <= pool.assets[market.borrow_unit]
        assert len(market.collateral) == len(pool.collateral_units)


def test_a_pool_read_alone_names_its_managing_script():
    market = _pool().to_market()
    assert market.lender == Party(
        PartyKind.SCRIPT,
        c.POOL_MANAGER_POLICY,
        "withdraw_script",
    )


def test_a_pool_refuses_another_pools_manager():
    pools = [_pool(0), _pool(1)]
    with pytest.raises(ValueError, match="does not manage"):
        pools[0].to_market(_manager_of(pools[1]))


def test_an_ada_pool_as_a_market():
    pool = _pool()
    market = pool.to_market(_manager_of(pool))
    others = {u: q for u, q in pool.assets.items() if u != "lovelace"}
    floor = min_output_lovelace(
        address=pool.address,
        assets=others,
        datum=RawCBOR(bytes.fromhex(pool.datum_cbor)),
        slot=0,
    )
    assert market.available_liquidity == pool.assets["lovelace"] - floor > 0
    common = pool.pool_datum.common_data
    assert market.rate_model == FluidPerpetualRate(
        interest_rate=common.interest_rate,
        total_installments=0,
        installment_period=0,
        initial_grace_period=0,
        repayment_time_window=0,
        penalty_fee_for_late_repayment=0,
        apy_coef=28,
    )
    assert market.borrow_rate == Fraction(common.interest_rate, 10_000)
    assert (market.supply_rate, market.receipt_unit, market.utilization) == (
        None,
        None,
        None,
    )
    assert market.on_default is DefaultRule.PRICE_LIQUIDATION
    assert market.permissioned is False
    (terms,) = market.collateral
    assert terms.unit == pool.collateral_units[0]
    assert terms.policy_wide is False
    assert (terms.max_ltv, terms.fixed_ratio) == (Fraction(2, 3), None)
    assert terms.liquidation_ltv == Fraction(4, 5)
    assert terms.liquidation_penalty == Fraction(1, 10)
    assert terms.liquidation_discount is None
    assert terms.equity_to_borrower is True
    feed = pool.pool_datum.collateral_options[0].oracle_token_asset
    assert terms.price_source is not None
    assert terms.price_source.source is OracleSource.FLUID_AGGREGATED
    assert (terms.price_source.token, terms.price_source.quote) == (
        terms.unit,
        "lovelace",
    )
    assert terms.price_source.feed_policy == feed.policy_id.hex()
    assert terms.price_source.feed_name == feed.asset_name.hex()


def test_a_token_pool_lends_its_whole_token_quantity():
    pool = _pool(_token_pool_index())
    assert lendable_principal(pool) == pool.assets[pool.borrowable_unit] > 0


def test_a_pool_below_its_minimum_ada_lends_nothing():
    rec = dict(FIX["pool"][ADA])
    rec["assets"] = dict(rec["assets"]) | {"lovelace": 1_000_000}
    market = FluidV4PoolState.from_record(record_info(rec)).to_market()
    assert market.available_liquidity == 0


def test_a_policy_wide_loan_holds_each_token_of_its_policy():
    rec = dict(FIX["loan"][0])
    datum = v4.LoanDatum.from_cbor(rec["datum_cbor"])
    policy = datum.collateral.policy_id.hex()
    wide = replace(datum.collateral, maybe_asset_name=_NONE)
    rec["datum_cbor"] = replace(datum, collateral=wide).to_cbor_hex()
    rec["assets"] = {
        u: q for u, q in dict(rec["assets"]).items() if not u.startswith(policy)
    } | {policy + "02": 3, policy + "01": 1}
    position = FluidV4LoanState.from_record(record_info(rec)).to_position()
    assert position.collateral == (
        CollateralHolding(policy + "01", 1),
        CollateralHolding(policy + "02", 3),
    )


def test_required_collateral_matches_the_borrow_builders_floor():
    for dynamic in (True, False):
        changes = {} if dynamic else {"dynamic_collateral_price": _FALSE}
        pool = _pool(**changes)
        market = pool.to_market()
        unit = market.collateral[0].unit
        book = prices((unit, "lovelace", 7, 1000))
        for principal in (1, 1_000_000, 123_456_789):
            assert market.required_collateral(
                unit,
                principal,
                book,
            ) == min_collateral_amount(
                pool.pool_datum,
                chosen_collateral_index=0,
                principal_amount=principal,
                price_num=7,
                price_den=1000,
            )


def test_a_pool_without_oracles_borrows_at_a_fixed_ratio():
    market = _pool(dynamic_collateral_price=_FALSE).to_market()
    (terms,) = market.collateral
    assert (terms.max_ltv, terms.fixed_ratio) == (None, Fraction(2, 3))
    assert terms.price_source is None
    assert market.required_collateral(terms.unit, 3) == 2


def test_a_policy_wide_collateral():
    datum = v4.PoolDatum.from_cbor(FIX["pool"][ADA]["datum_cbor"])
    (option,) = datum.collateral_options
    wide = replace(option, maybe_asset_name=_NONE)
    market = _pool(collateral_options=[wide]).to_market()
    (terms,) = market.collateral
    assert terms.policy_wide is True
    assert terms.unit == option.policy_id.hex()
    assert market.terms_for(option.policy_id.hex() + "0102") is terms


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        (v4.NoLiquidationFullCollateralClaim(), DefaultRule.FULL_COLLATERAL_CLAIM),
        (v4.NoLiquidationDutchAuctionClaim(), DefaultRule.DUTCH_AUCTION),
    ],
)
def test_pools_without_price_liquidation(mode, rule):
    market = _pool(common={"liquidation_mode": mode}).to_market()
    assert market.on_default is rule
    (terms,) = market.collateral
    assert terms.liquidation_ltv is None
    assert terms.liquidation_penalty is None
    assert terms.equity_to_borrower is False


def test_a_negative_penalty_forfeits_the_borrowers_equity():
    datum = v4.PoolDatum.from_cbor(FIX["pool"][ADA]["datum_cbor"])
    mode = replace(
        datum.common_data.liquidation_mode,
        partial_liquidation_penalty_per_mille=-1,
    )
    (terms,) = _pool(common={"liquidation_mode": mode}).to_market().collateral
    assert terms.liquidation_ltv == Fraction(4, 5)
    assert terms.liquidation_penalty is None
    assert terms.equity_to_borrower is False


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"min_collateral_divider": [0]}, "collateral ratio"),
        ({"min_collateral": [0]}, "collateral ratio"),
        ({"min_collateral": [100, 100]}, "pool"),
    ],
)
def test_an_unconvertible_pool_raises_a_parse_error(changes, match):
    with pytest.raises(LendingParseError, match=match):
        _pool(**changes).to_market()


@pytest.mark.parametrize(
    ("mode", "cls"),
    [
        (v4.PrincipalAndInterestOnInstallments(), FluidFlatTermRate),
        (v4.InterestOnRemainingPrincipal(max_possible_recasts=0), FluidAmortizedRate),
    ],
)
def test_installment_pools_carry_their_schedule(mode, cls):
    market = _pool(
        common={
            "repayment_mode": mode,
            "total_installments": 6,
            "installment_period": 720,
            "initial_grace_period": 24,
        },
    ).to_market()
    assert isinstance(market.rate_model, cls)
    assert market.rate_model.term_hours() == 24 + 6 * 720


def test_every_captured_loan_converts():
    for index in range(len(FIX["loan"])):
        loan = _loan(index)
        position = loan.to_position()
        assert position.position_id == c.LOAN_POLICY + loan.loan_id
        assert position.origin == Origin("market", c.POOL_POLICY + loan.pool_id)
        assert position.borrower == Party(
            PartyKind.TOKEN,
            c.BORROWER_BOND_POLICY + loan.loan_id,
        )
        assert position.lender == Party(
            PartyKind.TOKEN,
            c.LENDER_BOND_POLICY + loan.loan_id,
        )
        assert position.collateral == (
            CollateralHolding(loan.collateral_unit, loan.assets[loan.collateral_unit]),
        )
        assert position.expires_ms is None
        assert position.next_payment_due_ms is None


def test_position_debt_and_health_equal_the_loan_state():
    for index in range(len(FIX["loan"])):
        loan = _loan(index)
        position = loan.to_position()
        loan.set_time(NOW_MS)
        book = prices((loan.collateral_unit, loan.borrowed_unit, 13, 1000))
        assert position.current_debt(NOW_MS) == loan.current_debt()
        assert position.health_factor(book, NOW_MS) == loan.health_factor(book)
        assert position.is_liquidatable(book, NOW_MS) == loan.is_liquidatable(book)


@pytest.mark.parametrize(
    "mode",
    [
        v4.PrincipalAndInterestOnInstallments(),
        v4.InterestOnRemainingPrincipal(max_possible_recasts=0),
    ],
)
def test_installment_loans_owe_what_the_loan_state_owes(mode):
    loan = _loan(
        repayment_mode=mode,
        total_installments=6,
        installment_period=720,
        initial_grace_period=24,
        repaid_installments=2,
    )
    position = loan.to_position()
    loan.set_time(NOW_MS)
    assert position.current_debt(NOW_MS) == loan.current_debt()
    opened = position.opened_ms
    assert position.next_payment_due_ms == opened + (24 + 3 * 720) * 3_600_000
    assert position.expires_ms == opened + (24 + 6 * 720) * 3_600_000


def _request(**datum_changes):
    rec = v4_request_record()
    if datum_changes:
        datum = v4.RequestDatum.from_cbor(rec["datum_cbor"])
        rec["datum_cbor"] = replace(datum, **datum_changes).to_cbor_hex()
    return FluidV4RequestState.from_record(record_info(rec))


def test_a_request_as_a_borrow_request():
    state = _request()
    request = state.to_request()
    datum = state.request_datum
    assert request.request_id == c.REQUEST_POLICY + state.request_id
    assert request.borrower.kind in (PartyKind.KEY, PartyKind.SCRIPT)
    assert request.borrow_unit == datum.common_data.principal_asset.unit()
    assert request.collateral.unit == state.collateral_unit
    assert request.principal_max == datum.max_principal
    assert request.expires_ms == datum.request_expiration
    assert request.expiry_penalty == datum.request_expiration_penalty
    assert BorrowRequest.from_dict(request.to_dict()) == request


def test_a_fixed_ratio_request_takes_the_contracts_range():
    state = _request()
    collateral = 1_000_000
    request = replace(
        state.to_request(),
        collateral=CollateralHolding(state.collateral_unit, collateral),
    )
    assert request.principal_limit.fixed_ratio is not None
    assert request.principal_range() == request_principal_bounds(
        state.request_datum,
        collateral_amount=collateral,
    )
