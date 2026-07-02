"""Offline: RepaySnapshot.from_backend rebuilds the captured repay via a fake backend."""
from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_repay_from_backend_matches_capture(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        RepaySnapshot,
        _as_utxo,
    )

    fix = json.loads((_FIX / "repay_full.json").read_text())
    cap = RepaySnapshot.from_capture(fix)

    by_ref = {
        tuple(u["out_ref"]): _as_utxo(u)
        for u in fix["inputs"] + fix["ref_inputs"]
        if u.get("out_ref")
    }

    def fake_by_outref(backend, h, i, *, allow_spent=False):
        return by_ref[(h, i)]

    def fake_by_asset(backend, policy, name, *, allow_spent=False):
        return next(u for u in by_ref.values() if u.holds(policy, name))

    def fake_config(backend, *, allow_spent=False):
        return cap.config

    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_utxo_by_outref",
        fake_by_outref,
    )
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_utxo_by_asset",
        fake_by_asset,
    )
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_config_utxo",
        fake_config,
    )

    snap = RepaySnapshot.from_backend(
        None,
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
    assert snap.borrower_bond == cap.borrower_bond
    assert snap.lender_bond == cap.lender_bond
    assert snap.config == cap.config
    assert snap.spend_script_ref == cap.spend_script_ref
    assert snap.loan_policy_script_ref == cap.loan_policy_script_ref
    assert snap.repay_action_script_ref == cap.repay_action_script_ref
    assert snap.fee_address == cap.fee_address
    assert snap.fee_lovelace == cap.fee_lovelace
