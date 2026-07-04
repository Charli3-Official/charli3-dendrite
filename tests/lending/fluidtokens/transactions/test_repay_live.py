"""Gated: dbsync resolves a RepaySnapshot matching the capture; Ogmios evaluates it."""

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
def test_repay_from_backend_resolves_capture_fields() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import RepaySnapshot

    fix = json.loads((_FIX / "repay_full.json").read_text())
    cap = RepaySnapshot.from_capture(fix)
    backend = get_backend()
    snap = RepaySnapshot.from_backend(
        backend,
        loan_utxo=cap.loan.out_ref,
        actor_address=cap.borrower_bond.address,
        allow_spent=True,
        config_outref=cap.config.out_ref,
        spend_ref_outref=cap.spend_script_ref.out_ref,
        loan_policy_ref_outref=cap.loan_policy_script_ref.out_ref,
        repay_action_ref_outref=cap.repay_action_script_ref.out_ref,
        lender_bond_outref=cap.lender_bond.out_ref,
        borrower_bond_outref=cap.borrower_bond.out_ref,
        fee_address=cap.fee_address,
        fee_lovelace=cap.fee_lovelace,
    )
    assert snap.loan == cap.loan
    assert snap.loan_id == cap.loan_id
    assert snap.lender_bond == cap.lender_bond
    assert snap.borrower_bond == cap.borrower_bond
    assert snap.config == cap.config


@_gate()
def test_repay_from_backend_builds_and_evaluates() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        RepaySnapshot,
        _as_utxo,
        ogmios_entry,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.repay import build_repay
    from charli3_dendrite.lending.transactions.infra import (
        EvalContext,
        assemble_unsigned,
        evaluate_tx_cbor,
    )
    from pycardano import TransactionBuilder

    if not os.environ.get("OGMIOS_HOST"):
        pytest.skip("needs ogmios")

    fix = json.loads((_FIX / "repay_full.json").read_text())
    cap = RepaySnapshot.from_capture(fix)
    backend = get_backend()
    snap = RepaySnapshot.from_backend(
        backend,
        loan_utxo=cap.loan.out_ref,
        actor_address=cap.borrower_bond.address,
        allow_spent=True,
        config_outref=cap.config.out_ref,
        spend_ref_outref=cap.spend_script_ref.out_ref,
        loan_policy_ref_outref=cap.loan_policy_script_ref.out_ref,
        repay_action_ref_outref=cap.repay_action_script_ref.out_ref,
        lender_bond_outref=cap.lender_bond.out_ref,
        borrower_bond_outref=cap.borrower_bond.out_ref,
        fee_address=cap.fee_address,
        fee_lovelace=cap.fee_lovelace,
    )
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_repay(
        tx_builder,
        snapshot=snap,
        lender_lovelace=int(fix["outputs"][0]["lovelace"]),
    )
    additional = [ogmios_entry(_as_utxo(u)) for u in fix["inputs"] + fix["ref_inputs"]]
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), additional)
    assert budgets
    # A full repay drives four script executions: the loan spend, the loan-NFT
    # burn (mint), and the loan-policy + repay-action reward withdrawals.
    assert len(budgets) == 4
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
