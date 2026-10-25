"""The steps the flow builders share: one refusal for a market without loan scripts.

Every loan flow takes its loan address and loan-token policy through
``loan_target`` and every flow its reference scripts through ``required_script``,
so a snapshot lacking either is refused with ``UnsupportedMarketError`` before
anything is added to the builder.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from typing import Callable

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.transactions._flow import loan_target
from charli3_dendrite.lending.liqwid.transactions._flow import merge_validity_ms
from charli3_dendrite.lending.liqwid.transactions._flow import required_script
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import build_increase
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import build_collateral
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.repay import build_repay
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import build_supply
from charli3_dendrite.lending.transactions.errors import ValidityWindowError
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.units import script_payment_address
from charli3_dendrite.utility import posix_ms_to_slot
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import with_params

# (snapshot type, build, capture label) of each loan flow.
LOAN_FLOWS = [
    (BorrowSnapshot, build_borrow, "open_single"),
    (IncreaseSnapshot, build_increase, "increase_ada"),
    (RepaySnapshot, build_repay, "repay_usdm"),
    (RepaySnapshot, build_repay, "close_zero_debt"),
    (CollateralSnapshot, build_collateral, "collateral_add"),
]
LOAN_IDS = [label for _, _, label in LOAN_FLOWS]


def _builder() -> TransactionBuilder:
    return TransactionBuilder(EvalContext(last_block_slot=0))


def _refused(build: Callable[..., None], snapshot: Any, match: str) -> None:
    tx_builder = _builder()
    with pytest.raises(UnsupportedMarketError, match=match):
        build(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs
    assert not tx_builder.outputs
    assert not tx_builder.reference_inputs


@pytest.mark.parametrize(("kind", "build", "label"), LOAN_FLOWS, ids=LOAN_IDS)
def test_a_loan_flow_on_params_without_a_loan_validator_is_refused(
    kind,
    build,
    label,
):
    snapshot = with_params(kind.from_capture(fixture(label)), loan_validator=None)
    _refused(build, snapshot, "MarketParams name no loan validator")


@pytest.mark.parametrize(("kind", "build", "label"), LOAN_FLOWS, ids=LOAN_IDS)
def test_a_loan_flow_on_a_market_without_a_loan_token_policy_is_refused(
    kind,
    build,
    label,
):
    snapshot = kind.from_capture(fixture(label))
    market = dataclasses.replace(snapshot.market, new_borrow_policy=None)
    records = dataclasses.replace(snapshot.records, market=market)
    snapshot = dataclasses.replace(snapshot, market=market, records=records)
    _refused(build, snapshot, "no current loan-token policy")


@pytest.mark.parametrize(
    ("kind", "build", "label", "field", "what"),
    [
        (BorrowSnapshot, build_borrow, "open_single", "action", "action validator"),
        (BorrowSnapshot, build_borrow, "open_single", "loan_policy", "loan-token"),
        (BorrowSnapshot, build_borrow, "open_single", "borrow_staking", "staking"),
        (IncreaseSnapshot, build_increase, "increase_ada", "loan_validator", "loan v"),
        (RepaySnapshot, build_repay, "repay_usdm", "action", "action validator"),
        (RepaySnapshot, build_repay, "repay_usdm", "loan_validator", "loan validator"),
        (RepaySnapshot, build_repay, "close_zero_debt", "loan_policy", "loan-token"),
        (RepaySnapshot, build_repay, "close_zero_debt", "borrow_staking", "staking"),
        (SupplySnapshot, build_supply, "supply_ada", "action", "action validator"),
        (RedeemSnapshot, build_redeem, "redeem_ada", "qtoken_policy", "qToken"),
    ],
)
def test_a_missing_reference_script_is_refused_before_any_leg(
    kind,
    build,
    label,
    field,
    what,
):
    snapshot = kind.from_capture(fixture(label))
    refs = dataclasses.replace(snapshot.script_refs, **{field: None})
    _refused(build, dataclasses.replace(snapshot, script_refs=refs), what)


def test_the_loan_target_is_the_p35_enterprise_address_and_current_policy():
    snapshot = CollateralSnapshot.from_capture(fixture("collateral_add"))
    target = loan_target(snapshot.records)
    params = snapshot.records.params_datum
    assert params.loan_validator is not None
    assert target.address == script_payment_address(params.loan_validator)
    assert target.policy == snapshot.market.new_borrow_policy


def test_a_present_script_is_returned_as_it_is():
    snapshot = CollateralSnapshot.from_capture(fixture("collateral_add"))
    ref = snapshot.script_refs.loan_validator
    assert required_script(ref, snapshot.market, "loan validator") is ref


def test_validity_bounds_merge_on_either_side_alone():
    tx_builder = _builder()
    merge_validity_ms(tx_builder, upper_ms=None, lower_ms=None)
    assert (tx_builder.validity_start, tx_builder.ttl) == (None, None)
    merge_validity_ms(tx_builder, upper_ms=None, lower_ms=1_700_000_000_000)
    lower = posix_ms_to_slot(1_700_000_000_000)
    assert (tx_builder.validity_start, tx_builder.ttl) == (lower, None)
    merge_validity_ms(tx_builder, upper_ms=1_700_000_600_000, lower_ms=None)
    upper = posix_ms_to_slot(1_700_000_600_000)
    assert (tx_builder.validity_start, tx_builder.ttl) == (lower, upper)
    with pytest.raises(ValidityWindowError):
        merge_validity_ms(tx_builder, upper_ms=None, lower_ms=1_700_000_600_000)
    assert (tx_builder.validity_start, tx_builder.ttl) == (lower, upper)
