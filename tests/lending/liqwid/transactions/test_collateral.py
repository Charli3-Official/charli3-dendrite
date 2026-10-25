"""The Liqwid V2 collateral change: captured replays and the health edges.

A collateral change spends the loan and the owner's wallet, flips the loan token and
re-creates the loan with the target collateral (re-based when the loan index is
behind the market's). It spends no action UTxO and writes no validity window.
"""

import dataclasses
from fractions import Fraction

import pytest
from pycardano import PlutusV2Script
from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.transactions.collateral import (
    CollateralSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.collateral import build_collateral
from charli3_dendrite.lending.liqwid.transactions.errors import CollateralCountError
from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    UnsupportedCollateralError,
)
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanScriptRefs
from charli3_dendrite.lending.liqwid.transactions.terms import rebase_for_collateral
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.units import out_ref_text
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.liqwid.transactions.backend import capture_backend
from tests.lending.liqwid.transactions.replay import assert_replays
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import captured_protocol_outputs
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import protocol_outputs
from tests.lending.liqwid.transactions.replay import redeemers
from tests.lending.liqwid.transactions.replay import reference_inputs
from tests.lending.liqwid.transactions.replay import required_signers
from tests.lending.liqwid.transactions.replay import validity
from tests.lending.liqwid.transactions.replay import with_feed_price
from tests.lending.liqwid.transactions.replay import withdrawals

COLLATERAL_LABELS = [
    "collateral_add",
    "collateral_multi_add",
    "collateral_five",
    "collateral_remove",
    "collateral_remove_two",
    "collateral_drop_policy",
    "collateral_rebase",
    "family_isolated_collateral",
    "family_lp_collateral",
]

QBTC = "f72166e9fac8297aeb553c19ffab14f51ae271c2cb26783ba289a3a5"
QUSDM = "9e00df0615de0a7b121a7f961d43e23165b8e81b64786c6eb708d370"
BTC_FEED = "9cf4d4c472a4efe4f36c9c8c2a70872e21b40d5a446f3be90c918e9ab0b10a0f#9"
SLOT = 1_000


def _snapshot(label):
    return CollateralSnapshot.from_capture(fixture(label))


def _build(snapshot):
    return build(build_collateral, snapshot, slot=SLOT)


def _credentials(snapshot):
    """The loan validator's payment credential: the only protocol output's."""
    loan_validator = snapshot.records.params_datum.loan_validator
    assert loan_validator is not None
    return {loan_validator}


def _with_collateral(snapshot, **changes):
    collateral = dict(snapshot.collateral)
    collateral.update(changes)
    return dataclasses.replace(snapshot, collateral=collateral)


# --- replays -------------------------------------------------------------------


@pytest.mark.parametrize("label", COLLATERAL_LABELS)
def test_a_captured_collateral_change_replays(label):
    fix = fixture(label)
    snapshot = CollateralSnapshot.from_capture(fix)
    assert_replays(_build(snapshot), fix, credentials=_credentials(snapshot))


@pytest.mark.parametrize("label", COLLATERAL_LABELS)
def test_the_loan_record_written_is_the_rebase_of_the_input(label):
    snapshot = _snapshot(label)
    (output,) = protocol_outputs(_build(snapshot), _credentials(snapshot))
    s5 = snapshot.records.state_datum.index
    assert decode_loan(output.datum) == rebase_for_collateral(snapshot.loan.datum, s5)


def test_a_loan_behind_the_market_index_is_rebased():
    snapshot = _snapshot("collateral_rebase")
    loan = snapshot.loan.datum
    (output,) = protocol_outputs(_build(snapshot), _credentials(snapshot))
    written = decode_loan(output.datum)
    assert loan.index < snapshot.records.state_datum.index
    assert (written.principal, written.min_interest) == (
        loan.principal,
        loan.min_interest,
    )
    assert written.interest > loan.interest
    assert written.index > loan.index


@pytest.mark.parametrize("label", COLLATERAL_LABELS)
def test_a_collateral_change_writes_no_validity_and_requires_the_owner(label):
    snapshot = _snapshot(label)
    built = _build(snapshot)
    assert validity(built) == (None, None)
    assert required_signers(built) == [snapshot.loan.datum.owner]


@pytest.mark.parametrize("label", COLLATERAL_LABELS)
def test_the_loan_output_sits_at_the_p35_enterprise_address(label):
    snapshot = _snapshot(label)
    (output,) = protocol_outputs(_build(snapshot), _credentials(snapshot))
    loan_validator = snapshot.records.params_datum.loan_validator
    assert output.address == script_payment_address(loan_validator)


def test_the_loan_token_flips_and_the_owner_keeps_the_loan():
    snapshot = _snapshot("collateral_remove_two")
    policy = snapshot.market.new_borrow_policy
    assert snapshot.loan.token_name == "00"
    assert mints(_build(snapshot)) == {(policy + "00", -1), (policy + "01", 1)}


def test_only_the_funding_is_stashed_for_evaluation():
    snapshot = _snapshot("collateral_add")
    _build(snapshot)
    stashed = {
        f"{entry['transaction']['id']}#{entry['index']}"
        for entry in snapshot.additional_utxo()
    }
    assert stashed == {out_ref_text(u.out_ref) for u in snapshot.funding}
    assert out_ref_text(snapshot.loan.utxo.out_ref) not in stashed


def test_the_scripts_run_are_the_three_loan_scripts_only():
    snapshot = _snapshot("collateral_add")
    refs = snapshot.script_refs
    assert isinstance(refs, LoanScriptRefs)
    params = snapshot.records.params_datum
    assert script_hash_of(refs.loan_validator) == params.loan_validator
    assert script_hash_of(refs.loan_policy) == snapshot.market.new_borrow_policy
    assert {(purpose, cbor) for purpose, _, cbor in redeemers(_build(snapshot))} == {
        ("spend", "d87980"),
        ("mint", "d87980"),
        ("reward", "00"),
    }


def test_every_script_the_change_runs_is_plutus_v2():
    snapshot = _snapshot("collateral_five")
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=SLOT))
    build_collateral(tx_builder, snapshot=snapshot)
    scripts = [
        *tx_builder._reference_scripts,
        *tx_builder._inputs_to_scripts.values(),
        *(script for script, _ in tx_builder._minting_script_to_redeemers),
        *(script for script, _ in tx_builder._withdrawal_script_to_redeemers),
    ]
    assert len(tx_builder._reference_scripts) == 3
    assert all(isinstance(script, PlutusV2Script) for script in scripts)


# --- loan lovelace and datum style -----------------------------------------------


def test_from_capture_pins_the_captured_loan_lovelace():
    fix = fixture("family_isolated_collateral")
    snapshot = CollateralSnapshot.from_capture(fix)
    (captured,) = captured_protocol_outputs(fix, _credentials(snapshot))
    assert snapshot.loan_lovelace == captured.lovelace
    # Unpinned, the output holds the exact minimum (160 + S) * 4310 for its grown
    # record; the capture holds one byte's worth more.
    unpinned = dataclasses.replace(snapshot, loan_lovelace=None)
    (built,) = protocol_outputs(_build(unpinned), _credentials(snapshot))
    assert snapshot.loan.utxo.lovelace < built.lovelace == captured.lovelace - 4310


@pytest.mark.parametrize("label", COLLATERAL_LABELS)
def test_the_loan_lovelace_is_never_lowered(label):
    snapshot = dataclasses.replace(_snapshot(label), loan_lovelace=None)
    (output,) = protocol_outputs(_build(snapshot), _credentials(snapshot))
    assert output.lovelace >= snapshot.loan.utxo.lovelace


def test_the_loan_lovelace_rises_to_the_ledger_minimum():
    snapshot = _snapshot("collateral_add")
    snapshot = dataclasses.replace(snapshot, loan_lovelace=None)
    snapshot.loan = dataclasses.replace(
        snapshot.loan,
        utxo=dataclasses.replace(snapshot.loan.utxo, lovelace=1_000_000),
    )
    (output,) = protocol_outputs(_build(snapshot), _credentials(snapshot))
    assert output.lovelace > 1_000_000
    assert output.lovelace % 4310 == 0


def test_the_loan_datum_is_definite_unless_a_replay_pins_the_style():
    snapshot = _snapshot("collateral_add")
    assert snapshot.indefinite is False
    (definite,) = protocol_outputs(_build(snapshot), _credentials(snapshot))
    assert datum_style(definite.datum) == "definite"
    pinned = dataclasses.replace(snapshot, indefinite=True)
    (indefinite,) = protocol_outputs(_build(pinned), _credentials(pinned))
    assert datum_style(indefinite.datum) == "indefinite"


# --- health and limits -------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "unit", "market", "failing"),
    [
        ("collateral_remove", QBTC, "BTC", 1_371_905_771),
        ("collateral_remove_two", QUSDM, "USDM", 116_890_345_444),
    ],
)
def test_a_removal_is_held_to_the_exact_health_boundary(label, unit, market, failing):
    assert default_deployment().market_of_qtoken(unit).name == market
    snapshot = _snapshot(label)
    with pytest.raises(UnhealthyLoanError):
        _build(_with_collateral(snapshot, **{unit: failing}))
    built = _build(_with_collateral(snapshot, **{unit: failing + 1}))
    (output,) = protocol_outputs(built, _credentials(snapshot))
    assert (unit, failing + 1) in output.assets


def test_the_removal_boundary_reads_the_captured_btc_feed():
    snapshot = _snapshot("collateral_remove")
    underlying = snapshot.health.cparams[QBTC].datum.underlying
    assert snapshot.health.feeds[underlying].out_ref == BTC_FEED
    assert BTC_FEED in {out_ref_text(r) for r in reference_inputs(_build(snapshot))}


@pytest.mark.parametrize(
    ("price", "healthy"),
    [(695_635_307, False), (695_635_308, True)],
)
def test_an_add_is_held_to_the_health_rule_at_the_collateral_price(price, healthy):
    snapshot = _snapshot("collateral_add")
    underlying = snapshot.health.cparams[QBTC].datum.underlying
    priced = with_feed_price(snapshot, underlying, Fraction(price, 1_000_000))
    if healthy:
        _build(priced)
    else:
        with pytest.raises(UnhealthyLoanError):
            _build(priced)


def test_a_seventh_collateral_on_a_six_collateral_market_is_refused():
    snapshot = _snapshot("collateral_add")
    assert snapshot.records.params_datum.max_collaterals == 6
    extra = {f"{index:02x}" * 28: 1 for index in range(6)}
    with pytest.raises(CollateralCountError):
        _build(_with_collateral(snapshot, **extra))


def test_a_collateral_without_a_collateral_params_record_is_refused():
    snapshot = _snapshot("collateral_add")
    with pytest.raises(UnsupportedCollateralError):
        _build(_with_collateral(snapshot, **{"ab" * 28: 1}))


def test_a_zero_target_quantity_removes_the_unit():
    snapshot = _snapshot("collateral_drop_policy")
    removed = set(snapshot.loan.collateral) - set(snapshot.collateral)
    assert removed
    (unit,) = removed
    restated = _with_collateral(snapshot, **{unit: 0})
    assert_replays(
        _build(restated),
        fixture("collateral_drop_policy"),
        credentials=_credentials(snapshot),
    )


def test_a_target_equal_to_the_loan_s_collateral_is_refused():
    snapshot = _snapshot("collateral_add")
    unchanged = dataclasses.replace(snapshot, collateral=dict(snapshot.loan.collateral))
    with pytest.raises(ValueError, match="current one"):
        _build(unchanged)


def test_a_negative_target_quantity_is_refused_by_the_build():
    snapshot = _snapshot("collateral_add")
    (unit, _), *_ = snapshot.collateral.items()
    with pytest.raises(ValueError, match="negative collateral quantity"):
        _build(_with_collateral(snapshot, **{unit: -1}))


# --- live resolution ---------------------------------------------------------------


@pytest.mark.parametrize("label", COLLATERAL_LABELS)
def test_from_backend_builds_the_captured_change(label):
    fix = fixture(label)
    captured = CollateralSnapshot.from_capture(fix)
    snapshot = CollateralSnapshot.from_backend(
        capture_backend(fix),
        loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
        owner_address=captured.owner_address,
        collateral=captured.collateral,
        funding=captured.funding,
    )
    built, want = _build(snapshot), _build(captured)
    credentials = _credentials(snapshot)
    assert redeemers(built) == redeemers(want)
    assert mints(built) == mints(want)
    assert withdrawals(built) == withdrawals(want)
    assert required_signers(built) == required_signers(want)
    assert reference_inputs(built) == reference_inputs(want)
    assert validity(built) == (None, None)
    assert [o._replace(lovelace=0) for o in protocol_outputs(built, credentials)] == [
        o._replace(lovelace=0) for o in protocol_outputs(want, credentials)
    ]


def test_from_backend_reads_the_owner_wallet_when_no_funding_is_given():
    fix = fixture("collateral_add")
    captured = CollateralSnapshot.from_capture(fix)
    snapshot = CollateralSnapshot.from_backend(
        capture_backend(fix),
        loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
        owner_address=captured.owner_address,
        collateral=captured.collateral,
    )
    assert {u.out_ref for u in snapshot.funding} == {
        u.out_ref for u in captured.funding
    }


def test_from_backend_refuses_too_many_collaterals_before_reading_health():
    fix = fixture("collateral_add")
    captured = CollateralSnapshot.from_capture(fix)
    backend = capture_backend(fix)
    target = dict(captured.collateral)
    target.update({f"{index:02x}" * 28: 1 for index in range(6)})
    with pytest.raises(CollateralCountError):
        CollateralSnapshot.from_backend(
            backend,
            loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
            owner_address=captured.owner_address,
            collateral=target,
            funding=captured.funding,
        )
    oracles = {s.address for s in liqwid_selectors() if s.kind is LiqwidKind.ORACLE}
    read = {address for call in backend.calls for address in call["addresses"]}
    assert read
    assert not oracles & read


def test_from_backend_refuses_a_negative_target_before_any_read():
    fix = fixture("collateral_add")
    captured = CollateralSnapshot.from_capture(fix)
    backend = capture_backend(fix)
    (unit, _), *_ = captured.collateral.items()
    with pytest.raises(ValueError, match="negative collateral quantity"):
        CollateralSnapshot.from_backend(
            backend,
            loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
            owner_address=captured.owner_address,
            collateral={**captured.collateral, unit: -1},
            funding=captured.funding,
        )
    assert backend.calls == backend.queries == []


def test_from_backend_refuses_a_change_that_keeps_the_collateral():
    fix = fixture("collateral_add")
    captured = CollateralSnapshot.from_capture(fix)
    backend = capture_backend(fix)
    with pytest.raises(ValueError, match="current one"):
        CollateralSnapshot.from_backend(
            backend,
            loan_utxo=out_ref_text(captured.loan.utxo.out_ref),
            owner_address=captured.owner_address,
            collateral={**captured.loan.collateral, "ab" * 28: 0},
            funding=captured.funding,
        )
    assert backend.calls == []
