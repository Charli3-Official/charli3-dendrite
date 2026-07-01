"""Gated: dbsync resolves a LendSnapshot matching the captured request-fill."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

_FIX = Path(__file__).parent / "fixtures"


def _gate() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not os.environ.get("DBSYNC_HOST"),
        reason="needs dbsync",
    )


@_gate()
def test_resolve_request_utxo_matches_capture() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
    from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
        resolve_utxo_by_outref,
    )

    fix = json.loads((_FIX / "lend.json").read_text())
    cap = LendSnapshot.from_capture(fix)
    backend = get_backend()
    resolved = resolve_utxo_by_outref(backend, *cap.request.out_ref, allow_spent=True)
    assert resolved == cap.request


@_gate()
def test_lend_from_backend_resolves_capture_fields() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot

    fix = json.loads((_FIX / "lend.json").read_text())
    cap = LendSnapshot.from_capture(fix)
    backend = get_backend()
    snap = LendSnapshot.from_backend(
        backend,
        request_utxo=cap.request.out_ref,
        given_principal_amount=cap.given_principal_amount,
        allow_spent_request=True,
        funding_outrefs=[u.out_ref for u in cap.funding],
        config_outref=cap.config.out_ref,
        request_spend_ref_outref=cap.request_spend_script_ref.out_ref,
        request_policy_ref_outref=cap.request_policy_script_ref.out_ref,
        loan_policy_ref_outref=cap.loan_policy_script_ref.out_ref,
        lender_bond_ref_outref=cap.lender_bond_policy_script_ref.out_ref,
        borrower_bond_ref_outref=cap.borrower_bond_policy_script_ref.out_ref,
        valid_from=cap.valid_from,
        valid_to=cap.valid_to,
        loan_lovelace=cap.loan_lovelace,
    )
    assert snap.request == cap.request
    assert snap.config == cap.config
    assert snap.funding == cap.funding
    assert snap.request_id == cap.request_id
    assert snap.loan_id == cap.loan_id
    assert snap.borrower_address == cap.borrower_address
    assert snap.given_principal_amount == cap.given_principal_amount
    assert snap.collateral_unit == cap.collateral_unit
    assert snap.collateral_amount == cap.collateral_amount
    assert snap.request_spend_script_ref == cap.request_spend_script_ref
    assert snap.loan_policy_script_ref == cap.loan_policy_script_ref


@_gate()
def test_lend_from_backend_builds_and_evaluates() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        _as_utxo,
        ogmios_entry,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.lend import build_lend
    from charli3_dendrite.lending.transactions.infra import (
        EvalContext,
        assemble_unsigned,
        evaluate_tx_cbor,
    )
    from pycardano import TransactionBuilder

    if not os.environ.get("OGMIOS_HOST"):
        pytest.skip("needs ogmios")

    fix = json.loads((_FIX / "lend.json").read_text())
    cap = LendSnapshot.from_capture(fix)
    backend = get_backend()
    snap = LendSnapshot.from_backend(
        backend,
        request_utxo=cap.request.out_ref,
        given_principal_amount=cap.given_principal_amount,
        allow_spent_request=True,
        funding_outrefs=[u.out_ref for u in cap.funding],
        config_outref=cap.config.out_ref,
        request_spend_ref_outref=cap.request_spend_script_ref.out_ref,
        request_policy_ref_outref=cap.request_policy_script_ref.out_ref,
        loan_policy_ref_outref=cap.loan_policy_script_ref.out_ref,
        lender_bond_ref_outref=cap.lender_bond_policy_script_ref.out_ref,
        borrower_bond_ref_outref=cap.borrower_bond_policy_script_ref.out_ref,
        valid_from=cap.valid_from,
        valid_to=cap.valid_to,
        loan_lovelace=cap.loan_lovelace,
    )
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=cap.valid_from))
    build_lend(tx_builder, snapshot=snap)
    additional = [ogmios_entry(_as_utxo(u)) for u in fix["inputs"] + fix["ref_inputs"]]
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), additional)
    assert budgets
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
