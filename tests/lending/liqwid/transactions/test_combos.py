"""Liqwid V2 combinations: supply-and-borrow, remove-and-redeem, repay-and-redeem.

Each combination is the exact union of its legs: a supply then an open, a redeem
then a collateral change, or redeems then a repay, with one funding stash, one
owner signer, one borrow staking withdrawal, the references merged by out-ref and
the validity window ending by the batch deadline of each market whose action UTxO
is spent. The replays rebuild the captured combinations; the live builds resolve
them through an offline backend (a repay-and-redeem has no capture: it closes the
loan a captured supply-and-borrow opened).
"""

from __future__ import annotations

import dataclasses
import random
from types import SimpleNamespace

import pytest
from pycardano import ScriptHash
from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions import combos
from charli3_dendrite.lending.liqwid.transactions import resolve
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.combos import MAX_REDEEMS
from charli3_dendrite.lending.liqwid.transactions.combos import (
    RemoveAndRedeemSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    RepayAndRedeemSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    SupplyAndBorrowSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    build_remove_and_redeem,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    build_repay_and_redeem,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    build_supply_and_borrow,
)
from charli3_dendrite.lending.liqwid.transactions.errors import CapExceededError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import (
    MarketAwaitingBatchError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.terms import close_payment
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_payout
from charli3_dendrite.lending.liqwid.transactions.terms import supply_mint
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.errors import ValidityWindowError
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.units import out_ref_text
from charli3_dendrite.utility import posix_ms_to_slot
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.backend import capture_backend
from tests.lending.liqwid.transactions.replay import assert_replays
from tests.lending.liqwid.transactions.replay import block_slot
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import captured_mints
from tests.lending.liqwid.transactions.replay import captured_protocol_outputs
from tests.lending.liqwid.transactions.replay import captured_redeemers
from tests.lending.liqwid.transactions.replay import captured_reference_inputs
from tests.lending.liqwid.transactions.replay import captured_required_signers
from tests.lending.liqwid.transactions.replay import captured_withdrawals
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import fixture_utxos
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import protocol_outputs
from tests.lending.liqwid.transactions.replay import records_with_params
from tests.lending.liqwid.transactions.replay import redeemers
from tests.lending.liqwid.transactions.replay import reference_inputs
from tests.lending.liqwid.transactions.replay import required_signers
from tests.lending.liqwid.transactions.replay import validator_credentials
from tests.lending.liqwid.transactions.replay import validity
from tests.lending.liqwid.transactions.replay import with_params
from tests.lending.liqwid.transactions.replay import withdrawals
from tests.lending.liqwid.transactions.test_resolve import _reference_utxos
from tests.lending.liqwid.transactions.test_resolve import (  # noqa: F401 - fixture
    deployment_hashes,
)

SUPPLY_AND_BORROWS = ["supply_and_borrow_ada_night", "supply_and_borrow_usdt_usdc"]
REMOVE_AND_REDEEMS = ["remove_and_redeem_a", "remove_and_redeem_night"]
QADA = "a04ce7a52545e5e33c2867e148898d9e667a69602285f6a1298f9d68"


def _build(contribute, snapshot, fix):
    return build(contribute, snapshot, slot=block_slot(fix))


def _legs(snapshot):
    """A combination's two legs, the action-only leg first."""
    if isinstance(snapshot, SupplyAndBorrowSnapshot):
        return snapshot.supply, snapshot.borrow
    return snapshot.redeem, snapshot.collateral


def _credentials(snapshot):
    """The action and loan validator credentials of both legs' markets."""
    first, second = _legs(snapshot)
    return (validator_credentials(first) | validator_credentials(second)) - {None}


def _combination(label):
    """The snapshot class and build function of a captured combination."""
    if label in SUPPLY_AND_BORROWS:
        return SupplyAndBorrowSnapshot, build_supply_and_borrow
    return RemoveAndRedeemSnapshot, build_remove_and_redeem


def _supply_and_borrow(label):
    fix = fixture(label)
    return fix, SupplyAndBorrowSnapshot.from_capture(fix)


def _remove_and_redeem(label):
    fix = fixture(label)
    return fix, RemoveAndRedeemSnapshot.from_capture(fix)


# --- replays -------------------------------------------------------------------


@pytest.mark.parametrize("label", SUPPLY_AND_BORROWS)
def test_a_supply_and_borrow_replays_its_capture(label):
    fix, snapshot = _supply_and_borrow(label)
    built = _build(build_supply_and_borrow, snapshot, fix)
    assert_replays(built, fix, credentials=_credentials(snapshot))


@pytest.mark.parametrize("label", REMOVE_AND_REDEEMS)
def test_a_remove_and_redeem_replays_its_capture(label):
    fix, snapshot = _remove_and_redeem(label)
    built = _build(build_remove_and_redeem, snapshot, fix)
    assert_replays(built, fix, credentials=_credentials(snapshot))


@pytest.mark.parametrize("label", SUPPLY_AND_BORROWS)
def test_a_supply_and_borrow_capture_pins_its_legs(label):
    fix, snapshot = _supply_and_borrow(label)
    supply, borrow = snapshot.supply, snapshot.borrow
    capture = CapturedTx.of(fix)
    (wallet,) = [u for u in capture.inputs if not u.datum]
    # The loan holds exactly the qTokens the supply mints, at the full rate.
    assert borrow.collateral == {supply.market.qtoken_policy: supply.qtokens}
    rate = supply.records.state_datum.qtoken_rate
    assert supply.qtokens == supply_mint(supply.amount, rate)
    assert supply.action.out_ref != borrow.action.out_ref
    assert supply.funding == borrow.funding == [wallet]
    assert supply.owner_address == borrow.owner_address == wallet.address
    # The captured loan lovelace and lower bound are pinned on the open leg.
    (loan,) = capture.loan_outputs()
    assert borrow.loan_lovelace == loan.utxo.lovelace
    assert borrow.valid_from_ms == slot_to_posix_ms(fix["invalid_before"])


def test_the_over_funded_supply_and_borrow_keeps_its_captured_loan_lovelace():
    # Liqwid's rule gives (160 + S) * 4310; this capture's builder wrote 17 bytes'
    # worth more, which the replay keeps only because the snapshot pins it.
    fix, snapshot = _supply_and_borrow("supply_and_borrow_usdt_usdc")
    assert snapshot.borrow.loan_lovelace == 1_517_120
    snapshot.borrow.loan_lovelace = None
    built = _build(build_supply_and_borrow, snapshot, fix)
    loan_credential = snapshot.borrow.records.params_datum.loan_validator
    (written,) = protocol_outputs(built, {loan_credential})
    (captured,) = captured_protocol_outputs(fix, {loan_credential})
    assert written.lovelace < captured.lovelace
    assert written._replace(lovelace=captured.lovelace) == captured


@pytest.mark.parametrize("label", REMOVE_AND_REDEEMS)
def test_a_remove_and_redeem_burns_the_removed_qtokens_out_of_the_loan(label):
    fix, snapshot = _remove_and_redeem(label)
    collateral, redeem = snapshot.collateral, snapshot.redeem
    unit = redeem.market.qtoken_policy
    removed = collateral.loan.collateral[unit] - collateral.collateral[unit]
    assert removed == redeem.qtokens > 0
    rate = redeem.records.state_datum.qtoken_rate
    assert redeem.payout == redeem_payout(redeem.qtokens, rate)
    capture = CapturedTx.of(fix)
    (wallet,) = [u for u in capture.inputs if not u.datum]
    assert collateral.funding == redeem.funding == [wallet]
    assert collateral.owner_address == redeem.owner_address == wallet.address
    # No wallet input holds the qTokens burnt.
    assert not [a for a in wallet.assets if a[0] == unit]


@pytest.mark.parametrize("label", SUPPLY_AND_BORROWS + REMOVE_AND_REDEEMS)
def test_a_combination_signs_once_and_withdraws_once(label):
    fix = fixture(label)
    cls, contribute = _combination(label)
    built = _build(contribute, cls.from_capture(fix), fix)
    assert len(required_signers(built)) == 1
    assert len(withdrawals(built)) == 1
    assert len([r for r in redeemers(built) if r[0] == "spend"]) == 2


@pytest.mark.parametrize("label", SUPPLY_AND_BORROWS + REMOVE_AND_REDEEMS)
def test_only_the_funding_is_stashed_on_the_outer_snapshot(label):
    fix = fixture(label)
    cls, contribute = _combination(label)
    snapshot = cls.from_capture(fix)
    _build(contribute, snapshot, fix)
    capture = CapturedTx.of(fix)
    (wallet,) = [u for u in capture.inputs if not u.datum]
    assert snapshot.additional_utxo() == [ogmios_entry(wallet)]
    for leg in _legs(snapshot):
        with pytest.raises(RuntimeError):
            leg.additional_utxo()


# --- validity ------------------------------------------------------------------


@pytest.mark.parametrize("label", SUPPLY_AND_BORROWS + REMOVE_AND_REDEEMS)
def test_the_captured_bound_is_inside_both_markets_deadlines(label):
    fix = fixture(label)
    cls, _ = _combination(label)
    snapshot = cls.from_capture(fix)
    upper = slot_to_posix_ms(fix["invalid_hereafter"])
    for leg in _legs(snapshot):
        records = leg.records
        assert upper <= ttl_cap_ms(records.state_datum, records.params_datum)


def test_a_remove_and_redeem_bound_passes_the_loan_markets_deadline():
    # NIGHT's deadline is 443,000 ms past the captured bound and Ada's 11,718,000 ms:
    # a bound between the two is kept, since only the redeemed market's binds.
    fix, snapshot = _remove_and_redeem("remove_and_redeem_night")
    loan_records = snapshot.collateral.records
    redeem_records = snapshot.redeem.records
    night = ttl_cap_ms(loan_records.state_datum, loan_records.params_datum)
    ada = ttl_cap_ms(redeem_records.state_datum, redeem_records.params_datum)
    captured = slot_to_posix_ms(fix["invalid_hereafter"])
    assert (night - captured, ada - captured) == (443_000, 11_718_000)
    snapshot.redeem.valid_to_ms = captured + 1_000_000
    built = _build(build_remove_and_redeem, snapshot, fix)
    assert validity(built) == (None, posix_ms_to_slot(captured + 1_000_000))


def test_a_remove_and_redeem_bound_past_the_redeem_markets_deadline_is_refused():
    fix, snapshot = _remove_and_redeem("remove_and_redeem_night")
    redeem_records = snapshot.redeem.records
    ada = ttl_cap_ms(redeem_records.state_datum, redeem_records.params_datum)
    snapshot.redeem.valid_to_ms = ada + 1_000
    with pytest.raises(ValidityWindowError, match="batch deadline"):
        _build(build_remove_and_redeem, snapshot, fix)


def test_a_supply_and_borrow_bound_is_capped_at_the_borrow_markets_deadline():
    fix, snapshot = _supply_and_borrow("supply_and_borrow_ada_night")
    records = snapshot.borrow.records
    cap = ttl_cap_ms(records.state_datum, records.params_datum)
    supply_records = snapshot.supply.records
    assert cap < ttl_cap_ms(supply_records.state_datum, supply_records.params_datum)
    snapshot.borrow.valid_to_ms = cap + 60_000
    snapshot.supply.valid_to_ms = cap + 60_000
    built = _build(build_supply_and_borrow, snapshot, fix)
    assert validity(built)[1] == posix_ms_to_slot(cap)


# --- refusals ------------------------------------------------------------------


def _empty_builder():
    return TransactionBuilder(EvalContext(last_block_slot=0))


def test_a_loan_not_holding_exactly_the_supplied_qtokens_is_refused():
    _, snapshot = _supply_and_borrow("supply_and_borrow_ada_night")
    (unit,) = snapshot.borrow.collateral
    snapshot.borrow.collateral = {unit: snapshot.supply.qtokens - 1}
    tx_builder = _empty_builder()
    with pytest.raises(ValueError, match="qTokens the supply mints"):
        build_supply_and_borrow(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs


def test_a_supply_and_borrow_spending_one_action_utxo_twice_is_refused():
    _, snapshot = _supply_and_borrow("supply_and_borrow_ada_night")
    snapshot.borrow.action = snapshot.supply.action
    tx_builder = _empty_builder()
    with pytest.raises(ValueError, match="two different action UTxOs"):
        build_supply_and_borrow(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs


def test_a_removal_other_than_the_redeemed_qtokens_is_refused():
    _, snapshot = _remove_and_redeem("remove_and_redeem_night")
    snapshot.redeem.qtokens -= 1
    tx_builder = _empty_builder()
    with pytest.raises(ValueError, match="removes"):
        build_remove_and_redeem(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs


def test_legs_reading_two_states_of_one_market_are_refused():
    _, snapshot = _remove_and_redeem("remove_and_redeem_night")
    redeem = snapshot.redeem
    state = redeem.records.state
    index = state.info.tx_index + 1
    other = dataclasses.replace(
        state,
        out_ref=f"{state.info.tx_hash}#{index}",
        info=state.info.model_copy(update={"tx_index": index}),
    )
    snapshot.redeem = dataclasses.replace(
        redeem,
        records=dataclasses.replace(redeem.records, state=other),
    )
    tx_builder = _empty_builder()
    with pytest.raises(ValueError, match="MarketState"):
        build_remove_and_redeem(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs


# --- live resolution -----------------------------------------------------------


def _stand_in_backend(fix, *extra):
    """A backend serving a capture as live, other reference scripts as stand-ins."""
    held = {u.out_ref for u in CapturedTx.of(fix).references if u.ref_script}
    stand_ins = [u for u in _reference_utxos() if u.out_ref not in held]
    return capture_backend(fix, utxos=[*extra, *stand_ins])


def _assert_same_protocol_effects(built, fix):
    assert redeemers(built) == captured_redeemers(fix)
    assert mints(built) == captured_mints(fix)
    assert withdrawals(built) == captured_withdrawals(fix)
    assert required_signers(built) == captured_required_signers(fix)
    assert reference_inputs(built) == captured_reference_inputs(fix)


def test_a_supply_and_borrow_resolved_from_the_backend_builds_the_captured_one(
    deployment_hashes,
):
    fix, captured = _supply_and_borrow("supply_and_borrow_ada_night")
    snapshot = SupplyAndBorrowSnapshot.from_backend(
        _stand_in_backend(fix),
        supply_market="Ada",
        borrow_market="NIGHT",
        owner_address=captured.supply.owner_address,
        supply_amount=captured.supply.amount,
        borrow_amount=captured.borrow.amount,
        funding=captured.supply.funding,
        rng=random.Random(0),
    )
    assert snapshot.supply.action.out_ref == captured.supply.action.out_ref
    assert snapshot.borrow.action.out_ref == captured.borrow.action.out_ref
    assert snapshot.borrow.collateral == captured.borrow.collateral
    upper = min(snapshot.supply.valid_to_ms, snapshot.borrow.valid_to_ms)
    assert snapshot.supply.valid_to_ms == snapshot.borrow.valid_to_ms == upper
    built = _build(build_supply_and_borrow, snapshot, fix)
    _assert_same_protocol_effects(built, fix)
    actions = {
        snapshot.supply.records.params_datum.action_validator,
        snapshot.borrow.records.params_datum.action_validator,
    }
    # The captured action datums are indefinite; a resolved build writes them
    # definite, with the same fields.
    assert _decoded(protocol_outputs(built, actions)) == _decoded(
        captured_protocol_outputs(fix, actions),
    )
    assert {datum_style(o.datum) for o in protocol_outputs(built, actions)} == {
        "definite",
    }


def _decoded(views, decode=decode_action):
    """``views`` with each datum decoded, so the array style does not count."""
    return [o._replace(datum=decode(o.datum)) for o in views]


def test_a_remove_and_redeem_resolved_from_the_backend_builds_the_captured_one():
    fix, captured = _remove_and_redeem("remove_and_redeem_night")
    snapshot = RemoveAndRedeemSnapshot.from_backend(
        capture_backend(fix),
        loan_utxo=out_ref_text(captured.collateral.loan.utxo.out_ref),
        owner_address=captured.collateral.owner_address,
        qtoken_unit=QADA,
        qtokens=captured.redeem.qtokens,
        funding=captured.collateral.funding,
        valid_to_ms=captured.redeem.valid_to_ms,
        rng=random.Random(0),
    )
    assert snapshot.redeem.action.out_ref == captured.redeem.action.out_ref
    assert snapshot.redeem.payout == captured.redeem.payout
    assert snapshot.collateral.collateral == captured.collateral.collateral
    built = _build(build_remove_and_redeem, snapshot, fix)
    _assert_same_protocol_effects(built, fix)
    assert validity(built) == validity(_build(build_remove_and_redeem, captured, fix))
    # The captured datums are indefinite; a resolved build writes them definite,
    # with the same fields.
    action = {snapshot.redeem.records.params_datum.action_validator}
    loan = {snapshot.collateral.records.params_datum.loan_validator}
    assert _decoded(protocol_outputs(built, action)) == _decoded(
        captured_protocol_outputs(fix, action),
    )
    assert _decoded(protocol_outputs(built, loan), decode_loan) == _decoded(
        captured_protocol_outputs(fix, loan),
        decode_loan,
    )


@pytest.mark.parametrize("before_loan_deadline_s", [300, -60])
def test_a_live_remove_and_redeem_ends_by_the_redeem_markets_deadline(
    before_loan_deadline_s,
):
    fix, captured = _remove_and_redeem("remove_and_redeem_night")
    night = captured.collateral.records
    cap = ttl_cap_ms(night.state_datum, night.params_datum)
    # Five minutes before NIGHT's deadline, and a minute past it, the default
    # window (900 s) is kept: it stays well inside Ada's deadline, the only one
    # that binds.
    tip_s = cap // 1000 - before_loan_deadline_s
    snapshot = RemoveAndRedeemSnapshot.from_backend(
        capture_backend(fix, tip_time=tip_s),
        loan_utxo=out_ref_text(captured.collateral.loan.utxo.out_ref),
        owner_address=captured.collateral.owner_address,
        qtoken_unit=QADA,
        qtokens=captured.redeem.qtokens,
        funding=captured.collateral.funding,
    )
    ada = snapshot.redeem.records
    assert ttl_cap_ms(ada.state_datum, ada.params_datum) > tip_s * 1000 + 900_000
    assert snapshot.redeem.valid_to_ms == tip_s * 1000 + 900_000


def test_a_live_remove_and_redeem_of_more_than_the_loan_holds_is_refused():
    fix, captured = _remove_and_redeem("remove_and_redeem_night")
    held = captured.collateral.loan.collateral[QADA]
    with pytest.raises(ValueError, match="holds"):
        RemoveAndRedeemSnapshot.from_backend(
            capture_backend(fix),
            loan_utxo=out_ref_text(captured.collateral.loan.utxo.out_ref),
            owner_address=captured.collateral.owner_address,
            qtoken_unit=QADA,
            qtokens=held + 1,
            funding=captured.collateral.funding,
        )


def test_a_live_remove_and_redeem_of_a_unit_that_is_no_qtoken_is_refused():
    fix, captured = _remove_and_redeem("remove_and_redeem_night")
    with pytest.raises(ValueError, match="qToken"):
        RemoveAndRedeemSnapshot.from_backend(
            capture_backend(fix),
            loan_utxo=out_ref_text(captured.collateral.loan.utxo.out_ref),
            owner_address=captured.collateral.owner_address,
            qtoken_unit="00" * 28,
            qtokens=1,
            funding=captured.collateral.funding,
        )


def test_a_live_remove_and_redeem_paying_below_the_minimum_is_refused():
    fix, captured = _remove_and_redeem("remove_and_redeem_night")
    with pytest.raises(MinValueError):
        RemoveAndRedeemSnapshot.from_backend(
            capture_backend(fix),
            loan_utxo=out_ref_text(captured.collateral.loan.utxo.out_ref),
            owner_address=captured.collateral.owner_address,
            qtoken_unit=QADA,
            qtokens=1,
            funding=captured.collateral.funding,
        )


def test_a_supply_and_borrow_over_the_borrow_cap_reads_the_params_borrow_cap():
    fix, snapshot = _supply_and_borrow("supply_and_borrow_ada_night")
    snapshot.borrow = with_params(snapshot.borrow, borrow_cap=(0, 1))
    with pytest.raises(CapExceededError):
        _build(build_supply_and_borrow, snapshot, fix)


# --- one market --------------------------------------------------------------------

# One transaction spends at most one action UTxO of a market: the scripts refuse a
# supply-and-borrow on one market and a remove-and-redeem of the loan market's own
# qTokens (the Ogmios gates record both refusals).


@pytest.mark.parametrize(
    "action_utxos",
    [(None, None), ("aa" * 32 + "#0", "bb" * 32 + "#0")],
    ids=["chosen", "pinned"],
)
def test_a_supply_and_borrow_on_one_market_is_refused_before_any_read(action_utxos):
    backend = FixtureBackend()
    with pytest.raises(UnsupportedMarketError) as raised:
        SupplyAndBorrowSnapshot.from_backend(
            backend,
            supply_market="Ada",
            borrow_market="Ada",
            owner_address=SupplySnapshot.from_capture(
                fixture("supply_ada"),
            ).owner_address,
            supply_amount=1_000_000_000,
            borrow_amount=100_000_000,
            action_utxos=action_utxos,
        )
    assert raised.value.market == "Ada"
    assert "two markets" in raised.value.reason
    assert backend.calls == backend.queries == []


def test_a_built_supply_and_borrow_on_one_market_is_refused():
    # A supply to Ada and an open on Ada, each a captured leg of its own.
    supply = SupplySnapshot.from_capture(fixture("supply_ada"))
    borrow = BorrowSnapshot.from_capture(fixture("open_single"))
    assert supply.market.market_id == borrow.market.market_id
    borrow.collateral = {supply.market.qtoken_policy: supply.qtokens}
    snapshot = SupplyAndBorrowSnapshot(supply=supply, borrow=borrow)
    tx_builder = _empty_builder()
    with pytest.raises(UnsupportedMarketError, match="two markets"):
        build_supply_and_borrow(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs


def test_a_remove_and_redeem_of_the_loan_markets_qtoken_is_refused_after_the_loan():
    fix, captured = _remove_and_redeem("remove_and_redeem_night")
    night = captured.collateral.market
    backend = capture_backend(fix)
    with pytest.raises(UnsupportedMarketError) as raised:
        RemoveAndRedeemSnapshot.from_backend(
            backend,
            loan_utxo=out_ref_text(captured.collateral.loan.utxo.out_ref),
            owner_address=captured.collateral.owner_address,
            qtoken_unit=night.qtoken_policy,
            qtokens=1,
            funding=captured.collateral.funding,
        )
    assert raised.value.market == night.name
    assert "another market" in raised.value.reason
    # Only the loan is read: no market record and no wallet UTxO.
    assert backend.calls == []
    assert not [q for q in backend.queries if "addr" in q]


def test_a_built_remove_and_redeem_of_the_loan_markets_qtoken_is_refused():
    _, snapshot = _remove_and_redeem("remove_and_redeem_night")
    night = snapshot.collateral.market
    snapshot.redeem = dataclasses.replace(snapshot.redeem, market=night)
    tx_builder = _empty_builder()
    with pytest.raises(UnsupportedMarketError, match="another market"):
        build_remove_and_redeem(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs


# --- repay-and-redeem ----------------------------------------------------------

# A close of the NIGHT loan ``supply_and_borrow_ada_night`` opened, redeeming its
# qADA on Ada: no capture holds one, so it is resolved live from that capture's
# Ada and NIGHT records and action UTxOs, with the NIGHT loan validator's
# reference script from ``remove_and_redeem_night`` (the supply-and-borrow spends
# no NIGHT loan) and stand-ins for the other reference scripts. Every test that
# reads this backend takes the ``deployment_hashes`` fixture.


def _closing_night_loan(collateral=QADA, extra=None):
    """The NIGHT loan of ``supply_and_borrow_ada_night`` and a backend to close it.

    ``collateral`` stands in for the loan's qADA, at the same quantity; ``extra``
    (unit: quantity, empty asset names) is posted beside it.
    """
    fix = fixture("supply_and_borrow_ada_night")
    (loan,) = CapturedTx.of(fix).loan_outputs()
    if collateral != QADA or extra:
        assets = [
            (collateral, n, q) if p == QADA else (p, n, q)
            for p, n, q in loan.utxo.assets
        ]
        assets += [(unit, "", q) for unit, q in (extra or {}).items()]
        loan = dataclasses.replace(
            loan,
            utxo=dataclasses.replace(loan.utxo, assets=sorted(assets)),
        )
    scripts = [
        u for u in fixture_utxos(fixture("remove_and_redeem_night")) if u.ref_script
    ]
    return fix, _stand_in_backend(fix, loan.utxo, *scripts), loan


def _repay_and_redeem(fix, backend, loan, **kwargs):
    """A close of ``loan`` and redeems of what it releases, from ``backend``."""
    captured = SupplyAndBorrowSnapshot.from_capture(fix).borrow
    kwargs.setdefault(
        "repay",
        close_payment(loan.datum, captured.records.state_datum.index),
    )
    kwargs.setdefault("rng", random.Random(0))
    return RepayAndRedeemSnapshot.from_backend(
        backend,
        loan_utxo=out_ref_text(loan.utxo.out_ref),
        owner_address=captured.owner_address,
        funding=captured.funding,
        **kwargs,
    )


def _serve_params(monkeypatch, market, fields):
    """Serve ``market``'s MarketParams with the raw ``fields`` (index: value)."""
    real = resolve.resolve_market

    def patched(backend, components, **kwargs):
        records = real(backend, components, **kwargs)
        if components.name != market:
            return records
        raw = list(records.params_datum.raw)
        for index, value in fields.items():
            raw[index] = value
        return records_with_params(records, raw=tuple(raw))

    monkeypatch.setattr(resolve, "resolve_market", patched)


def _qtoken(name):
    return default_deployment().market_by_name(name).qtoken_policy


def test_a_repay_and_redeem_redeems_the_released_qada_out_of_the_closing_loan(
    deployment_hashes,
):
    fix, backend, loan = _closing_night_loan()
    snapshot = _repay_and_redeem(fix, backend, loan)
    q = loan.collateral[QADA]
    rate = SupplyAndBorrowSnapshot.from_capture(fix).supply.records.state_datum
    assert snapshot.repay.closes
    (redeem,) = snapshot.redeems
    assert (redeem.market.name, redeem.qtokens) == ("Ada", q)
    assert redeem.payout == redeem_payout(q, rate.qtoken_rate)
    assert snapshot.returned == {}
    built = _build(build_repay_and_redeem, snapshot, fix)
    # The qADA is burned straight out of the closing loan: no wallet input or
    # output holds it.
    assert not [a for u in snapshot.repay.funding for a in u.assets if a[0] == QADA]
    qada = ScriptHash(bytes.fromhex(QADA))
    assert not [
        o for o in built.tx.transaction_body.outputs if qada in o.amount.multi_asset
    ]
    loan_token = loan.market.new_borrow_policy + loan.token_name
    assert mints(built) == {(QADA, -q), (loan_token, -1)}
    assert required_signers(built) == [loan.datum.owner]
    assert len(withdrawals(built)) == 1
    assert len([r for r in redeemers(built) if r[0] == "spend"]) == 3


def test_the_redeem_legs_come_before_the_repay_leg(deployment_hashes, monkeypatch):
    fix, backend, loan = _closing_night_loan()
    snapshot = _repay_and_redeem(fix, backend, loan)
    order = []
    for name, leg in (("redeem", combos._build_redeem), ("repay", combos._build_repay)):

        def spy(*args, _name=name, _leg=leg, **kwargs):
            order.append(_name)
            return _leg(*args, **kwargs)

        monkeypatch.setattr(combos, f"_build_{name}", spy)
    _build(build_repay_and_redeem, snapshot, fix)
    assert order == ["redeem", "repay"]


def test_only_the_funding_is_stashed_on_a_repay_and_redeem(deployment_hashes):
    fix, backend, loan = _closing_night_loan()
    snapshot = _repay_and_redeem(fix, backend, loan)
    _build(build_repay_and_redeem, snapshot, fix)
    (wallet,) = snapshot.repay.funding
    assert snapshot.additional_utxo() == [ogmios_entry(wallet)]
    for leg in (*snapshot.redeems, snapshot.repay):
        with pytest.raises(RuntimeError):
            leg.additional_utxo()


@pytest.mark.parametrize("requested", [False, True], ids=["default", "requested"])
def test_the_bound_is_the_earliest_of_both_markets_deadlines(
    deployment_hashes,
    requested,
):
    fix, backend, loan = _closing_night_loan()
    sab = SupplyAndBorrowSnapshot.from_capture(fix)
    night, ada = sab.borrow.records, sab.supply.records
    night_cap = ttl_cap_ms(night.state_datum, night.params_datum)
    # Five minutes before NIGHT's deadline the default window (900 s) passes it
    # and stays well inside Ada's.
    tip_s = night_cap // 1000 - 300
    assert tip_s * 1000 + 900_000 < ttl_cap_ms(ada.state_datum, ada.params_datum)
    backend.tip_time = tip_s
    snapshot = _repay_and_redeem(
        fix,
        backend,
        loan,
        valid_to_ms=tip_s * 1000 + 900_000 if requested else None,
    )
    # A requested bound is capped at the deadline; the default ends a second
    # before it.
    upper = night_cap if requested else night_cap - 1_000
    (redeem,) = snapshot.redeems
    assert snapshot.repay.valid_to_ms == redeem.valid_to_ms == upper
    built = _build(build_repay_and_redeem, snapshot, fix)
    assert validity(built)[1] == posix_ms_to_slot(upper)


def test_a_redeem_markets_earlier_deadline_binds_the_repay_leg(
    deployment_hashes,
    monkeypatch,
):
    fix, backend, loan = _closing_night_loan()
    sab = SupplyAndBorrowSnapshot.from_capture(fix)
    night, ada = sab.borrow.records, sab.supply.records
    tip_ms = backend.tip_time * 1000
    assert ttl_cap_ms(night.state_datum, night.params_datum) > tip_ms + 900_000
    # Ada's deadline moved five minutes past the tip, inside the default window
    # NIGHT's leaves open.
    ada_cap = tip_ms + 300_000
    _serve_params(monkeypatch, "Ada", {3: ada_cap - ada.state_datum.last_batch_ms})
    snapshot = _repay_and_redeem(fix, backend, loan)
    (redeem,) = snapshot.redeems
    assert snapshot.repay.valid_to_ms == redeem.valid_to_ms == ada_cap - 1_000
    built = _build(build_repay_and_redeem, snapshot, fix)
    assert validity(built)[1] == posix_ms_to_slot(ada_cap - 1_000)


def test_a_leg_without_a_bound_is_given_none():
    unbounded, late, early = (
        SimpleNamespace(valid_to_ms=bound) for bound in (None, 9_000, 4_000)
    )
    combos._end_together(unbounded, late, early)
    assert (unbounded.valid_to_ms, late.valid_to_ms, early.valid_to_ms) == (
        None,
        4_000,
        4_000,
    )
    alone = SimpleNamespace(valid_to_ms=None)
    combos._end_together(alone)
    assert alone.valid_to_ms is None


def test_a_released_unit_paying_below_the_minimum_is_returned(
    deployment_hashes,
    monkeypatch,
):
    fix, backend, loan = _closing_night_loan()
    q = loan.collateral[QADA]
    rate = SupplyAndBorrowSnapshot.from_capture(fix).supply.records.state_datum
    _serve_params(monkeypatch, "Ada", {1: redeem_payout(q, rate.qtoken_rate) + 1})
    snapshot = _repay_and_redeem(fix, backend, loan)
    assert snapshot.redeems == ()
    assert snapshot.returned == {QADA: q}
    with pytest.raises(MinValueError):
        _repay_and_redeem(fix, backend, loan, redeem=[QADA])


def test_a_redeem_market_awaiting_its_batch_is_refused_not_returned(
    deployment_hashes,
    monkeypatch,
):
    fix, backend, loan = _closing_night_loan()
    sab = SupplyAndBorrowSnapshot.from_capture(fix)
    night, ada = sab.borrow.records, sab.supply.records
    tip_ms = backend.tip_time * 1000
    assert ttl_cap_ms(night.state_datum, night.params_datum) > tip_ms + 900_000
    # Ada's deadline moved to the tip; NIGHT's stays open.
    _serve_params(monkeypatch, "Ada", {3: tip_ms - ada.state_datum.last_batch_ms})
    with pytest.raises(MarketAwaitingBatchError) as raised:
        _repay_and_redeem(fix, backend, loan)
    assert raised.value.market == "Ada"


def test_the_loan_markets_own_qtoken_is_returned_in_auto_mode_and_refused_when_named(
    deployment_hashes,
):
    qnight = _qtoken("NIGHT")
    fix, backend, loan = _closing_night_loan(collateral=qnight)
    assert loan.collateral == {qnight: loan.collateral[qnight]}
    snapshot = _repay_and_redeem(fix, backend, loan)
    assert snapshot.redeems == ()
    assert snapshot.returned == loan.collateral
    with pytest.raises(UnsupportedMarketError) as raised:
        _repay_and_redeem(fix, backend, loan, redeem=[qnight])
    assert raised.value.market == "NIGHT"
    assert "another market" in raised.value.reason


def test_a_released_unit_no_action_utxo_can_pay_out_is_returned(
    deployment_hashes,
    monkeypatch,
):
    fix, backend, loan = _closing_night_loan()
    q = loan.collateral[QADA]
    real = resolve.resolve_flow_action

    def drained(backend, records, **kwargs):
        if records.market.name == "Ada":
            raise InsufficientLiquidityError(q, 0)
        return real(backend, records, **kwargs)

    monkeypatch.setattr(resolve, "resolve_flow_action", drained)
    snapshot = _repay_and_redeem(fix, backend, loan)
    assert snapshot.redeems == ()
    assert snapshot.returned == {QADA: q}
    with pytest.raises(InsufficientLiquidityError):
        _repay_and_redeem(fix, backend, loan, redeem=[QADA])


def test_redeems_are_capped(deployment_hashes):
    fix, backend, loan = _closing_night_loan()
    snapshot = _repay_and_redeem(fix, backend, loan, max_redeems=0)
    assert snapshot.redeems == ()
    assert snapshot.returned == snapshot.repay.released_collateral == loan.collateral


@pytest.mark.parametrize("max_redeems", [MAX_REDEEMS, 1])
def test_units_skipped_beside_a_redeemed_one_are_returned(
    deployment_hashes,
    max_redeems,
):
    # A unit that is no qToken sorts before the qADA and the loan market's own
    # qNIGHT after it: both are returned, neither counts against the cap, and the
    # qADA between them is redeemed.
    other, qnight = "00" * 28, _qtoken("NIGHT")
    fix, backend, loan = _closing_night_loan(extra={other: 5, qnight: 77})
    assert sorted(loan.collateral) == [other, QADA, qnight]
    snapshot = _repay_and_redeem(fix, backend, loan, max_redeems=max_redeems)
    assert [r.market.name for r in snapshot.redeems] == ["Ada"]
    assert snapshot.returned == {other: 5, qnight: 77}
    built = _build(build_repay_and_redeem, snapshot, fix)
    loan_token = loan.market.new_borrow_policy + loan.token_name
    assert mints(built) == {(QADA, -loan.collateral[QADA]), (loan_token, -1)}


def test_a_partial_repay_redeems_what_its_target_releases(deployment_hashes):
    fix, backend, loan = _closing_night_loan()
    q = loan.collateral[QADA]
    index = SupplyAndBorrowSnapshot.from_capture(fix).borrow.records.state_datum.index
    snapshot = _repay_and_redeem(
        fix,
        backend,
        loan,
        repay=close_payment(loan.datum, index) // 2,
        collateral={QADA: q - q // 10},
    )
    assert not snapshot.repay.closes
    (redeem,) = snapshot.redeems
    assert (redeem.market.name, redeem.qtokens) == ("Ada", q // 10)
    assert snapshot.returned == {}
    built = _build(build_repay_and_redeem, snapshot, fix)
    # The loan stays open (its loan token is re-minted); only the released qADA
    # is burned.
    assert {m for m in mints(built) if m[0] == QADA} == {(QADA, -(q // 10))}


@pytest.mark.parametrize(
    ("redeem", "match"),
    [
        (["00" * 28], "not a Liqwid qToken"),
        ([QADA, QADA], "one redeem per market"),
        ([QADA, _qtoken("NIGHT"), _qtoken("USDT"), _qtoken("USDC")], "at most 3"),
    ],
    ids=["no-qtoken", "one-market-twice", "over-the-cap"],
)
def test_named_redeems_are_refused_before_any_read(redeem, match):
    backend = FixtureBackend()
    with pytest.raises(ValueError, match=match):
        RepayAndRedeemSnapshot.from_backend(
            backend,
            loan_utxo="aa" * 32 + "#0",
            owner_address=SupplySnapshot.from_capture(
                fixture("supply_ada"),
            ).owner_address,
            repay=1,
            redeem=redeem,
        )
    assert backend.calls == backend.queries == []


def test_a_named_unit_the_repay_does_not_release_is_refused(deployment_hashes):
    fix, backend, loan = _closing_night_loan()
    usdt = _qtoken("USDT")
    with pytest.raises(ValueError, match=f"does not release {usdt}"):
        _repay_and_redeem(fix, backend, loan, redeem=[QADA, usdt])


def test_a_built_redeem_burning_other_than_released_is_refused(deployment_hashes):
    fix, backend, loan = _closing_night_loan()
    snapshot = _repay_and_redeem(fix, backend, loan, redeem=[QADA])
    (redeem,) = snapshot.redeems
    q = loan.collateral[QADA]
    snapshot.redeems = (dataclasses.replace(redeem, qtokens=q - 1),)
    tx_builder = _empty_builder()
    with pytest.raises(ValueError, match=f"releases {q} .* burns {q - 1}"):
        build_repay_and_redeem(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs


@pytest.mark.parametrize(
    ("conflict", "error", "match"),
    [
        (lambda snapshot, redeem: (redeem, redeem), ValueError, "two redeems"),
        (
            lambda snapshot, redeem: (
                dataclasses.replace(redeem, action=snapshot.repay.action),
            ),
            ValueError,
            "two different action UTxOs",
        ),
        (
            lambda snapshot, redeem: (
                dataclasses.replace(redeem, market=snapshot.repay.market),
            ),
            UnsupportedMarketError,
            "another market",
        ),
    ],
    ids=["one-market-twice", "the-repays-action", "the-loans-market"],
)
def test_a_built_repay_and_redeem_with_conflicting_legs_is_refused(
    deployment_hashes,
    conflict,
    error,
    match,
):
    fix, backend, loan = _closing_night_loan()
    snapshot = _repay_and_redeem(fix, backend, loan, redeem=[QADA])
    (redeem,) = snapshot.redeems
    snapshot.redeems = conflict(snapshot, redeem)
    tx_builder = _empty_builder()
    with pytest.raises(error, match=match):
        build_repay_and_redeem(tx_builder, snapshot=snapshot)
    assert not tx_builder.inputs
