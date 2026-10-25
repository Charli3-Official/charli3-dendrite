"""Liqwid V2 repay: partial repays, closes and the zero-debt close.

Each captured repay and close replays byte-exactly from its capture; the synthetic
cases change one quantity of a capture and check what the builder writes.
"""

from __future__ import annotations

import dataclasses
import random

import pytest
from pycardano import Address

from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.datums import encode_loan_datum
from charli3_dendrite.lending.liqwid.deployment import staking_roles
from charli3_dendrite.lending.liqwid.transactions import resolve
from charli3_dendrite.lending.liqwid.transactions import terms
from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
from charli3_dendrite.lending.liqwid.transactions.errors import RepayAmountError
from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.repay import build_repay
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.units import out_ref_text
from charli3_dendrite.utility import posix_ms_to_slot
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.liqwid.records import records_of
from tests.lending.liqwid.transactions.backend import capture_backend
from tests.lending.liqwid.transactions.replay import assert_replays
from tests.lending.liqwid.transactions.replay import block_slot
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import protocol_outputs
from tests.lending.liqwid.transactions.replay import reference_inputs
from tests.lending.liqwid.transactions.replay import required_signers
from tests.lending.liqwid.transactions.replay import validator_credentials
from tests.lending.liqwid.transactions.replay import validity
from tests.lending.liqwid.transactions.replay import withdrawals

PARTIAL_REPAYS = (
    "repay_usdm",
    "repay_clamp",
    "repay_interest_only",
    "repay_with_collateral",
    "family_isolated_repay",
    "family_lp_repay",
)
CLOSES = (
    "close_ada",
    "close_top_up",
    "family_isolated_close",
    "family_lp_close",
    "family_staked_close",
)
ZERO_DEBT_CLOSE = "close_zero_debt"
# A live BTC loan that owes only its minimum interest, (0, 0, 82).
MINIMUM_ONLY_LOAN = "027c8b4f62b81f6c116951a5210b05d18213961673af455d91a5356ffb4b5bbf#1"


# --- helpers -------------------------------------------------------------------


def _snapshot(label):
    return RepaySnapshot.from_capture(fixture(label))


def _build(snapshot, fix):
    return build(build_repay, snapshot, slot=block_slot(fix))


def _s5(snapshot):
    return snapshot.records.state_datum.index


def _with_loan(snapshot, datum):
    """``snapshot`` with its loan's record replaced by ``datum``."""
    loan = snapshot.loan
    utxo = dataclasses.replace(loan.utxo, datum=encode_loan_datum(datum).hex())
    return dataclasses.replace(
        snapshot,
        loan=dataclasses.replace(loan, utxo=utxo, datum=datum),
    )


def _view(built, credential):
    (view,) = protocol_outputs(built, {credential})
    return view


def _action_view(built, snapshot):
    return _view(built, snapshot.records.params_datum.action_validator)


def _loan_view(built, snapshot):
    return _view(built, snapshot.records.params_datum.loan_validator)


def _capture_backend(fix):
    """An offline backend holding what the capture spent and referenced, unspent."""
    return capture_backend(fix, tip_time=fix["block_time"] - 60)


def _captured_scripts(monkeypatch, captured):
    """Serve ``captured``'s reference scripts in place of the deployment's.

    The captures hold only the scripts each transaction ran, while live resolution
    also reads the market's qToken and liquidation staking scripts.
    """
    calls = []

    def scripts(backend, market, params, **kwargs):
        calls.append((market, kwargs))
        return captured.script_refs

    monkeypatch.setattr(resolve, "resolve_script_refs", scripts)
    return calls


def _owner_address(snapshot):
    """The captured wallet address paying with the loan owner's key."""
    owner = snapshot.loan.datum.owner
    return next(
        u.address
        for u in snapshot.funding
        if Address.decode(u.address).payment_part.payload.hex() == owner
    )


# --- replays -------------------------------------------------------------------


@pytest.mark.parametrize("label", [*PARTIAL_REPAYS, *CLOSES, ZERO_DEBT_CLOSE])
def test_captured_repays_replay(label):
    fix = fixture(label)
    snapshot = RepaySnapshot.from_capture(fix)
    assert_replays(
        _build(snapshot, fix), fix, credentials=validator_credentials(snapshot)
    )


@pytest.mark.parametrize("label", PARTIAL_REPAYS)
def test_a_partial_repay_capture_pays_at_most_the_settled_debt(label):
    snapshot = _snapshot(label)
    settlement = terms.settle(snapshot.loan.datum, _s5(snapshot))
    assert not snapshot.closes
    assert 0 < snapshot.repay <= settlement.debt
    assert snapshot.action is not None


@pytest.mark.parametrize("label", CLOSES)
def test_a_close_capture_pays_exactly_the_payoff(label):
    snapshot = _snapshot(label)
    assert snapshot.closes
    assert snapshot.repay == terms.close_payment(snapshot.loan.datum, _s5(snapshot))


def test_the_zero_debt_close_capture_spends_no_action_utxo():
    snapshot = _snapshot(ZERO_DEBT_CLOSE)
    loan = snapshot.loan.datum
    assert (loan.principal, loan.interest, loan.min_interest) == (0, 0, 0)
    assert snapshot.closes
    assert snapshot.action is None
    assert snapshot.valid_to_ms is None


# --- closes --------------------------------------------------------------------


def test_a_repay_above_the_payoff_closes_with_exactly_the_payoff():
    fix = fixture("close_ada")
    snapshot = _snapshot("close_ada")
    payoff = terms.close_payment(snapshot.loan.datum, _s5(snapshot))
    more = dataclasses.replace(snapshot, repay=payoff + 5_000_000)
    assert more.closes
    built = _build(more, fix)
    assert_replays(built, fix, credentials=validator_credentials(snapshot))
    action = snapshot.action
    assert _action_view(built, snapshot).lovelace == action.utxo.lovelace + payoff


@pytest.mark.parametrize("label", ["close_ada", ZERO_DEBT_CLOSE])
def test_a_close_requires_the_owner_and_withdraws_from_borrow_staking(label):
    fix = fixture(label)
    snapshot = _snapshot(label)
    built = _build(snapshot, fix)
    roles = staking_roles(snapshot.market, snapshot.records.params_datum)
    assert required_signers(built) == [snapshot.loan.datum.owner]
    assert withdrawals(built) == {(roles.borrow, 0)}
    token = snapshot.market.new_borrow_policy + snapshot.loan.token_name
    assert mints(built) == {(token, -1)}
    assert protocol_outputs(built, {snapshot.records.params_datum.loan_validator}) == []


def test_a_minimum_interest_only_loan_closes_through_an_action_utxo():
    (record,) = [r for r in records_of("loan") if r["out_ref"] == MINIMUM_ONLY_LOAN]
    owed = decode_loan(record["datum_cbor"])
    assert (owed.principal, owed.interest, owed.min_interest) == (0, 0, 82)
    fix = fixture("close_ada")
    snapshot = _with_loan(_snapshot("close_ada"), owed)
    snapshot = dataclasses.replace(snapshot, repay=1_000_000)
    assert snapshot.closes
    built = _build(snapshot, fix)
    action = _action_view(built, snapshot)
    before = snapshot.action
    assert action.lovelace == before.utxo.lovelace + 82
    deltas = ActionDeltas.between(before.datum, decode_action(action.datum))
    assert deltas == ActionDeltas(min_interest_paid=82)
    assert required_signers(built) == [owed.owner]
    assert validity(built) == (None, posix_ms_to_slot(snapshot.valid_to_ms))


def test_the_zero_debt_close_writes_no_validity():
    fix = fixture(ZERO_DEBT_CLOSE)
    built = _build(_snapshot(ZERO_DEBT_CLOSE), fix)
    assert validity(built) == (None, None)


def test_the_zero_debt_close_references_the_loan_scripts_and_market_records():
    fix = fixture(ZERO_DEBT_CLOSE)
    snapshot = _snapshot(ZERO_DEBT_CLOSE)
    refs = snapshot.script_refs
    expected = {
        refs.loan_validator.out_ref,
        refs.loan_policy.out_ref,
        refs.borrow_staking.out_ref,
        tuple(snapshot.records.params.out_ref.split("#")),
        tuple(snapshot.records.state.out_ref.split("#")),
    }
    built = _build(snapshot, fix)
    assert reference_inputs(built) == {(h, int(i)) for h, i in expected}


def test_a_close_refuses_a_collateral_target():
    snapshot = dataclasses.replace(_snapshot("close_ada"), collateral={})
    fix = fixture("close_ada")
    with pytest.raises(ValueError, match="collateral"):
        _build(snapshot, fix)


# --- partial repays ------------------------------------------------------------


def test_a_partial_repay_below_the_market_minimum_is_refused():
    fix = fixture("family_isolated_repay")
    snapshot = _snapshot("family_isolated_repay")
    minimum = snapshot.records.params_datum.min_value
    assert minimum - 1 < terms.settle(snapshot.loan.datum, _s5(snapshot)).debt
    with pytest.raises(MinValueError):
        _build(dataclasses.replace(snapshot, repay=minimum - 1), fix)


def test_repaying_the_settled_debt_leaves_a_minimum_interest_only_loan():
    fix = fixture("repay_clamp")
    snapshot = _snapshot("repay_clamp")
    loan = snapshot.loan.datum
    settlement = terms.settle(loan, _s5(snapshot))
    assert loan.min_interest > settlement.interest
    exact = dataclasses.replace(snapshot, repay=settlement.debt)
    assert not exact.closes
    built = _build(exact, fix)
    assert decode_loan(_loan_view(built, snapshot).datum) == LoanDatum(
        loan.owner,
        0,
        0,
        loan.min_interest - settlement.interest,
        settlement.index,
    )
    over = dataclasses.replace(snapshot, repay=settlement.debt + 1)
    assert not over.closes
    with pytest.raises(RepayAmountError):
        _build(over, fix)


def test_a_collateral_removal_that_leaves_an_unhealthy_loan_is_refused():
    fix = fixture("repay_with_collateral")
    snapshot = _snapshot("repay_with_collateral")
    unit, held = next(iter(snapshot.collateral.items()))
    drained = dataclasses.replace(snapshot, collateral={unit: held // 1000})
    with pytest.raises(UnhealthyLoanError):
        _build(drained, fix)


# --- from_backend --------------------------------------------------------------


def test_from_backend_rebuilds_a_captured_partial_repay(monkeypatch):
    fix = fixture("repay_usdm")
    captured = _snapshot("repay_usdm")
    calls = _captured_scripts(monkeypatch, captured)
    backend = _capture_backend(fix)
    snapshot = RepaySnapshot.from_backend(
        backend,
        loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
        owner_address=_owner_address(captured),
        repay=captured.repay,
        valid_to_ms=slot_to_posix_ms(fix["invalid_hereafter"]),
        rng=random.Random(0),
    )
    assert calls == [(captured.market, {"deployment": None})]
    assert snapshot.action == captured.action
    assert snapshot.health is not None
    assert sorted(u.out_ref for u in snapshot.funding) == sorted(
        u.out_ref for u in captured.funding
    )
    assert_replays(
        _build(snapshot, fix), fix, credentials=validator_credentials(captured)
    )


def test_from_backend_rebuilds_the_zero_debt_close_without_an_action_utxo(
    monkeypatch,
):
    fix = fixture(ZERO_DEBT_CLOSE)
    captured = _snapshot(ZERO_DEBT_CLOSE)
    _captured_scripts(monkeypatch, captured)
    backend = _capture_backend(fix)
    snapshot = RepaySnapshot.from_backend(
        backend,
        loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
        owner_address=_owner_address(captured),
        repay=0,
    )
    assert snapshot.action is None
    assert snapshot.health is None
    assert snapshot.valid_to_ms is None
    assert all(call["assets"] is None for call in backend.calls)
    assert_replays(
        _build(snapshot, fix), fix, credentials=validator_credentials(captured)
    )


def test_from_backend_refuses_a_repay_between_the_settled_debt_and_the_payoff():
    fix = fixture("repay_clamp")
    captured = _snapshot("repay_clamp")
    debt = terms.settle(captured.loan.datum, _s5(captured)).debt
    backend = _capture_backend(fix)
    with pytest.raises(RepayAmountError):
        RepaySnapshot.from_backend(
            backend,
            loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
            owner_address=_owner_address(captured),
            repay=debt + 1,
        )
    loan_hash = captured.loan.utxo.out_ref[0]
    assert {q["h"] for q in backend.queries if "h" in q} == {loan_hash}
    assert all(call["assets"] is None for call in backend.calls)
