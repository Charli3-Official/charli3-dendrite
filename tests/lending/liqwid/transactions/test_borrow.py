"""Liqwid V2 borrow (open a loan) and borrow more: replays, limits and health edges."""

from __future__ import annotations

import dataclasses
import random
from fractions import Fraction

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import TransactionBuilder
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.deployment import staking_roles
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import build_increase
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.errors import CapExceededError
from charli3_dendrite.lending.liqwid.transactions.errors import CollateralCountError
from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market_records
from charli3_dendrite.lending.liqwid.transactions.terms import health_debt
from charli3_dendrite.lending.liqwid.transactions.terms import settle
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.utility import posix_ms_to_slot
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.backend import capture_backend
from tests.lending.liqwid.transactions.backend import live_records
from tests.lending.liqwid.transactions.replay import assert_replays
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import captured_mints
from tests.lending.liqwid.transactions.replay import captured_protocol_outputs
from tests.lending.liqwid.transactions.replay import captured_redeemers
from tests.lending.liqwid.transactions.replay import captured_reference_inputs
from tests.lending.liqwid.transactions.replay import captured_required_signers
from tests.lending.liqwid.transactions.replay import captured_withdrawals
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import protocol_outputs
from tests.lending.liqwid.transactions.replay import redeemers
from tests.lending.liqwid.transactions.replay import reference_inputs
from tests.lending.liqwid.transactions.replay import required_signers
from tests.lending.liqwid.transactions.replay import validator_credentials
from tests.lending.liqwid.transactions.replay import validity
from tests.lending.liqwid.transactions.replay import with_feed_price
from tests.lending.liqwid.transactions.replay import with_params
from tests.lending.liqwid.transactions.replay import withdrawals
from tests.lending.liqwid.transactions.test_resolve import _reference_utxos
from tests.lending.liqwid.transactions.test_resolve import (  # noqa: F401 - fixture
    deployment_hashes,
)

OPENS = [
    "open_single",
    "open_three",
    "open_liquidation_first",
    "open_at_limit",
    "open_indefinite",
    "family_isolated_open",
]
INCREASES = [
    "increase_ada",
    "increase_usdm",
    "increase_with_collateral",
    "family_lp_increase",
    "family_staked_increase",
]
OWNER = str(
    Address(payment_part=VerificationKeyHash(b"\x07" * 28), network=Network.MAINNET),
)


def _build(contribute, snapshot, fix):
    return build(contribute, snapshot, slot=fix["invalid_hereafter"])


def _market(name):
    return default_deployment().market_by_name(name)


# --- replays -------------------------------------------------------------------


@pytest.mark.parametrize("label", OPENS)
def test_an_open_replays_its_capture(label):
    fix = fixture(label)
    snapshot = BorrowSnapshot.from_capture(fix)
    built = _build(build_borrow, snapshot, fix)
    assert_replays(built, fix, credentials=validator_credentials(snapshot))


@pytest.mark.parametrize("label", INCREASES)
def test_a_borrow_more_replays_its_capture(label):
    fix = fixture(label)
    snapshot = IncreaseSnapshot.from_capture(fix)
    built = _build(build_increase, snapshot, fix)
    assert_replays(built, fix, credentials=validator_credentials(snapshot))


def test_an_open_on_a_liquidation_first_market_withdraws_from_the_borrow_script():
    fix = fixture("open_liquidation_first")
    snapshot = BorrowSnapshot.from_capture(fix)
    params = snapshot.records.params_datum
    roles = staking_roles(snapshot.market, params)
    assert params.withdraw_zero is not None
    assert roles.borrow == params.withdraw_zero[1] != params.withdraw_zero[0]
    assert snapshot.script_refs.borrow_staking is not None
    assert script_hash_of(snapshot.script_refs.borrow_staking) == roles.borrow
    built = _build(build_borrow, snapshot, fix)
    assert withdrawals(built) == {(roles.borrow, 0)}


def test_a_capture_holds_only_the_scripts_its_transaction_runs():
    open_refs = BorrowSnapshot.from_capture(fixture("open_single")).script_refs
    assert open_refs.qtoken_policy is None
    assert open_refs.loan_validator is None
    assert open_refs.liquidation_staking is None
    increase_refs = IncreaseSnapshot.from_capture(fixture("increase_ada")).script_refs
    assert increase_refs.loan_validator is not None


def test_an_indefinite_capture_writes_both_datums_indefinite():
    snapshot = BorrowSnapshot.from_capture(fixture("open_indefinite"))
    assert snapshot.indefinite is True
    assert BorrowSnapshot.from_capture(fixture("open_single")).indefinite is False


# --- limits and health ------------------------------------------------------------


def test_one_more_unit_on_an_open_at_the_limit_is_unhealthy():
    snapshot = BorrowSnapshot.from_capture(fixture("open_at_limit"))
    snapshot.amount += 1
    with pytest.raises(UnhealthyLoanError):
        _build(build_borrow, snapshot, fixture("open_at_limit"))


def test_the_borrow_cap_binds_at_the_captured_utilisation():
    # increase_ada lends 103 ADA from an action UTxO with no pending principal; the
    # per-UTxO utilisation after it is 3813458396338/9212231794565.
    fix = fixture("increase_ada")
    n = 413_955_975_205_470_918_050_687
    below = with_params(IncreaseSnapshot.from_capture(fix), borrow_cap=(n, 10**24))
    with pytest.raises(CapExceededError) as caught:
        _build(build_increase, below, fix)
    assert caught.value.cap == "borrow"
    at = with_params(IncreaseSnapshot.from_capture(fix), borrow_cap=(n + 1, 10**24))
    assert_replays(
        _build(build_increase, at, fix), fix, credentials=validator_credentials(at)
    )


def _scaled_price(snapshot, underlying, factor):
    feed = snapshot.health.feeds[underlying]
    price = Fraction(feed.usd_num, feed.usd_den) * factor
    return with_feed_price(snapshot, underlying, price)


def _staked_collateral_underlying(snapshot):
    (unit,) = snapshot.collateral_out
    return snapshot.health.cparams[unit].datum.underlying


def test_the_staked_borrow_more_measures_the_settled_debt():
    snapshot = IncreaseSnapshot.from_capture(fixture("family_staked_increase"))
    loan = snapshot.loan.datum
    settled = settle(loan, snapshot.records.state_datum.index)
    debt = health_debt(
        loan.principal,
        settled.interest,
        loan.min_interest,
        snapshot.amount,
        0,
    )
    # p0 + max(iS, m0) + A with m0 > iS.
    assert loan.min_interest > settled.interest
    assert debt == 20_999_332_479


def test_the_staked_borrow_more_sits_two_parts_in_1e11_inside_its_limit():
    # The captured collateral covers at most a debt of 167994659835331/8000
    # (20,999,332,479.42): the captured debt passes by a relative 3331/
    # 167994659832000 (about 1.98e-11) of the collateral price.
    fix = fixture("family_staked_increase")
    snapshot = IncreaseSnapshot.from_capture(fix)
    underlying = _staked_collateral_underlying(snapshot)
    one = _scaled_price(snapshot, underlying, 1 - Fraction(1, 10**11))
    assert_replays(
        _build(build_increase, one, fix), fix, credentials=validator_credentials(one)
    )
    snapshot = IncreaseSnapshot.from_capture(fix)
    two = _scaled_price(snapshot, underlying, 1 - Fraction(2, 10**11))
    with pytest.raises(UnhealthyLoanError):
        _build(build_increase, two, fix)


def test_one_more_unit_on_the_staked_borrow_more_is_unhealthy():
    fix = fixture("family_staked_increase")
    snapshot = IncreaseSnapshot.from_capture(fix)
    snapshot.amount += 1
    with pytest.raises(UnhealthyLoanError):
        _build(build_increase, snapshot, fix)


def test_a_validity_bound_past_the_batch_deadline_is_capped():
    fix = fixture("open_single")
    snapshot = BorrowSnapshot.from_capture(fix)
    cap = ttl_cap_ms(snapshot.records.state_datum, snapshot.records.params_datum)
    snapshot.valid_to_ms = cap + 60_000
    assert validity(_build(build_borrow, snapshot, fix)) == (
        None,
        posix_ms_to_slot(cap),
    )


# --- loan-token names ------------------------------------------------------------


def test_a_borrow_more_on_a_31_loan_burns_it_and_mints_01():
    fix = fixture("increase_ada")
    snapshot = IncreaseSnapshot.from_capture(fix)
    policy = snapshot.market.new_borrow_policy
    loan = snapshot.loan
    renamed = dataclasses.replace(
        loan.utxo,
        assets=[(p, "31" if p == policy else n, q) for p, n, q in loan.utxo.assets],
    )
    snapshot.loan = LoanRecord(
        utxo=renamed,
        datum=loan.datum,
        market=loan.market,
        token_name="31",
    )
    built = _build(build_increase, snapshot, fix)
    assert mints(built) == {(policy + "31", -1), (policy + "01", 1)}
    (loan_out,) = [
        v
        for v in protocol_outputs(built, validator_credentials(snapshot))
        if (policy + "01", 1) in v.assets
    ]
    assert decode_loan(loan_out.datum).owner == loan.datum.owner


# --- live resolution ------------------------------------------------------------


def _capture_backend(fix):
    """A backend serving a capture's records, scripts and spent inputs as live.

    Every other reference script the deployment lists is a stand-in at its
    recorded out-ref.
    """
    held = {u.out_ref for u in CapturedTx.of(fix).references if u.ref_script}
    stand_ins = [u for u in _reference_utxos() if u.out_ref not in held]
    return capture_backend(fix, utxos=stand_ins)


def _assert_same_protocol_effects(built, fix, snapshot):
    assert redeemers(built) == captured_redeemers(fix)
    assert mints(built) == captured_mints(fix)
    assert withdrawals(built) == captured_withdrawals(fix)
    assert required_signers(built) == captured_required_signers(fix)
    assert reference_inputs(built) == captured_reference_inputs(fix)


def test_an_open_resolved_from_the_backend_builds_the_captured_open(
    deployment_hashes,
):
    fix = fixture("open_single")
    captured = BorrowSnapshot.from_capture(fix)
    backend = _capture_backend(fix)
    snapshot = BorrowSnapshot.from_backend(
        backend,
        market="Ada",
        owner_address=captured.owner_address,
        amount=captured.amount,
        collateral=captured.collateral,
        funding=captured.funding,
        rng=random.Random(0),
    )
    assert snapshot.action.out_ref == captured.action.out_ref
    assert snapshot.owner == captured.owner
    built = _build(build_borrow, snapshot, fix)
    _assert_same_protocol_effects(built, fix, snapshot)
    loan_credential = snapshot.records.params_datum.loan_validator
    want = captured_protocol_outputs(fix, {loan_credential})
    got = protocol_outputs(built, {loan_credential})
    # Liqwid's app funds a new loan one byte's worth (4310 lovelace) above the least
    # lovelace; a resolved open holds the least.
    assert [v._replace(lovelace=v.lovelace + 4310) for v in got] == want
    action_credential = snapshot.records.params_datum.action_validator
    assert protocol_outputs(built, {action_credential}) == (
        captured_protocol_outputs(fix, {action_credential})
    )


def test_a_borrow_more_resolved_from_the_backend_builds_the_captured_one(
    deployment_hashes,
):
    fix = fixture("increase_ada")
    captured = IncreaseSnapshot.from_capture(fix)
    backend = _capture_backend(fix)
    snapshot = IncreaseSnapshot.from_backend(
        backend,
        loan_utxo="{}#{}".format(*captured.loan.utxo.out_ref),
        owner_address=captured.owner_address,
        amount=captured.amount,
        funding=captured.funding,
        rng=random.Random(0),
    )
    assert snapshot.action.out_ref == captured.action.out_ref
    built = _build(build_increase, snapshot, fix)
    _assert_same_protocol_effects(built, fix, snapshot)
    assert protocol_outputs(built, validator_credentials(snapshot)) == (
        captured_protocol_outputs(fix, validator_credentials(snapshot))
    )


def _live_backend():
    return FixtureBackend(records=live_records(), utxos=_reference_utxos())


def test_an_open_on_a_market_without_collateral_slots_is_refused(deployment_hashes):
    snek2 = _market("SNEK2")
    with pytest.raises(CollateralCountError) as caught:
        BorrowSnapshot.from_backend(
            _live_backend(),
            market="SNEK2",
            owner_address=OWNER,
            amount=10_000_000,
            collateral={_market("Ada").qtoken_policy: 1_000_000},
            funding=[],
        )
    assert (caught.value.count, caught.value.limit) == (1, 0)
    assert snek2.new_borrow_policy is not None


def test_an_open_on_a_market_without_loan_scripts_is_refused(deployment_hashes):
    with pytest.raises(UnsupportedMarketError) as caught:
        BorrowSnapshot.from_backend(
            _live_backend(),
            market="POL",
            owner_address=OWNER,
            amount=100_000_000,
            collateral={_market("Ada").qtoken_policy: 1_000_000},
            funding=[],
        )
    assert caught.value.market == "POL"


def test_a_borrow_below_the_market_minimum_is_refused_before_any_action_utxo(
    deployment_hashes,
):
    # The live records hold no action UTxO: the market-level limit refuses first.
    ada = default_deployment().market_by_name("Ada")
    params = resolve_market_records(_live_backend(), [ada])["Ada"].params_datum
    with pytest.raises(MinValueError) as caught:
        BorrowSnapshot.from_backend(
            _live_backend(),
            market="Ada",
            owner_address=OWNER,
            amount=params.min_value - 1,
            collateral={_market("DJED").qtoken_policy: 1_000_000},
            funding=[],
        )
    assert caught.value.minimum == params.min_value


def test_a_missing_loan_script_is_refused_before_the_builder_changes():
    fix = fixture("increase_ada")
    snapshot = IncreaseSnapshot.from_capture(fix)
    snapshot.script_refs = dataclasses.replace(
        snapshot.script_refs,
        loan_validator=None,
    )
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=0))
    with pytest.raises(UnsupportedMarketError, match="loan validator"):
        build_increase(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs
    assert not tx_builder.outputs
