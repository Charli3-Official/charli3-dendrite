"""A captured Liqwid V2 transaction read back into snapshot pieces."""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.capture import sole
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market_records
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.replay import fixture


def _ada():
    return default_deployment().market_by_name("Ada")


def test_the_records_are_the_ones_live_resolution_reads():
    capture = CapturedTx.of(fixture("open_single"))
    backend = FixtureBackend(records=[utxo_to_info(u) for u in capture.references])
    live = resolve_market_records(backend, [_ada()])["Ada"]
    assert capture.market_records(_ada()) == live


def test_scripts_are_found_by_hash_and_a_missing_one_is_none():
    capture = CapturedTx.of(fixture("open_single"))
    params = capture.market_records(_ada()).params_datum
    assert params.action_validator is not None
    action = capture.script(params.action_validator)
    assert action is not None
    assert script_hash_of(action) == params.action_validator
    assert capture.script(_ada().qtoken_policy) is None


def test_the_action_and_loan_are_spent_and_written_once():
    capture = CapturedTx.of(fixture("increase_ada"))
    (action_in,) = capture.action_inputs(_ada())
    (action_out,) = capture.action_outputs(_ada())
    (loan_in,) = capture.loan_inputs()
    (loan_out,) = capture.loan_outputs()
    assert action_out.datum.principal_delta - action_in.datum.principal_delta == (
        loan_out.datum.principal - loan_in.datum.principal
    )
    assert (loan_in.token_name, loan_out.token_name) == ("00", "01")
    assert loan_in.collateral == loan_out.collateral


def test_a_transaction_funded_by_the_borrow_alone_has_no_funding():
    capture = CapturedTx.of(fixture("increase_ada"))
    (action,) = capture.action_inputs(_ada())
    (loan,) = capture.loan_inputs()
    assert capture.funding(action.utxo, loan.utxo) == []
    (written,) = capture.loan_outputs()
    (recreated,) = capture.action_outputs(_ada())
    (change,) = [
        u
        for u in capture.outputs
        if u.out_ref not in {written.utxo.out_ref, recreated.utxo.out_ref}
    ]
    assert capture.wallet_address(
        action.utxo, loan.utxo, written.utxo, recreated.utxo
    ) == (change.address)


def test_the_validity_bounds_read_as_posix_milliseconds():
    fix = fixture("open_single")
    capture = CapturedTx.of(fix)
    assert capture.upper_bound_ms() == slot_to_posix_ms(fix["invalid_hereafter"])
    assert capture.valid_from_ms is None


def test_a_capture_without_an_upper_bound_has_none_to_give():
    capture = CapturedTx.of(fixture("close_zero_debt"))
    assert capture.valid_to_ms is None
    with pytest.raises(ValueError, match="no upper validity bound"):
        capture.upper_bound_ms()


def test_sole_names_what_it_did_not_find_once():
    assert sole([7], "action input") == 7
    with pytest.raises(
        ValueError, match="expected one loan input in the capture, found 0"
    ):
        sole([], "loan input")
    with pytest.raises(ValueError, match="found 2"):
        sole([1, 2], "loan input")


def test_the_qtoken_market_is_the_one_whose_qtokens_are_minted():
    assert CapturedTx.of(fixture("supply_ada")).qtoken_market() == _ada()
    with pytest.raises(ValueError, match="one market's qTokens"):
        CapturedTx.of(fixture("open_single")).qtoken_market()


def test_a_required_script_the_capture_lacks_is_refused_by_name():
    capture = CapturedTx.of(fixture("open_single"))
    params = capture.market_records(_ada()).params_datum
    assert capture.script_refs(_ada(), params).qtoken_policy is None
    with pytest.raises(ValueError, match="no qToken policy of Ada"):
        capture.script_refs(_ada(), params, require=("action", "qtoken_policy"))
    assert capture.script_refs(_ada(), params, require=()).action is not None


def test_the_written_style_is_indefinite_when_any_written_datum_is():
    indefinite = CapturedTx.of(fixture("open_indefinite"))
    (loan,) = indefinite.loan_outputs()
    assert indefinite.indefinite(None, loan.utxo) is True
    definite = CapturedTx.of(fixture("open_single"))
    (loan,) = definite.loan_outputs()
    assert definite.indefinite(loan.utxo) is False
    assert definite.indefinite() is False


def test_the_capture_keeps_its_mints_and_required_signers():
    fix = fixture("supply_ada")
    capture = CapturedTx.of(fix)
    assert capture.required_signers == tuple(fix["required_signers"])
    assert {(p + n, q) for p, n, q in capture.mints} == {
        (p + n, int(q)) for p, n, q in fix["mints"]
    }
