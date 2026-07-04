"""Offline: RecastSnapshot.from_backend rebuilds the captured recast via a fake backend."""
from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_recast_from_backend_matches_capture(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        RecastSnapshot,
        _as_utxo,
    )

    fix = json.loads((_FIX / "recast.json").read_text())
    cap = RecastSnapshot.from_capture(fix)

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

    def fake_funding(backend, address, *, limit=20):
        return [cap.funding]

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
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_funding",
        fake_funding,
    )

    snap = RecastSnapshot.from_backend(
        None,
        loan_utxo=cap.loan.out_ref,
        actor_address=cap.borrower_bond.address,
        amount_paid=cap.amount_paid,
        allow_spent=True,
        valid_from=cap.valid_from,
        valid_to=cap.valid_to,
        config_outref=cap.config.out_ref,
        spend_ref_outref=cap.spend_script_ref.out_ref,
        loan_policy_ref_outref=cap.loan_policy_script_ref.out_ref,
        action_ref_outref=cap.action_script_ref.out_ref,
        lender_bond_outref=cap.lender_bond.out_ref,
        borrower_bond_outref=cap.borrower_bond.out_ref,
        funding_outref=cap.funding.out_ref,
    )

    assert snap.new_principal_amount == cap.new_principal_amount
    assert snap.new_lend_date == cap.new_lend_date
    assert snap.lender_address == cap.lender_address
    assert snap.fee_address == cap.fee_address
    assert snap.fee_lovelace == cap.fee_lovelace
    assert snap.amount_paid == cap.amount_paid
    assert snap.valid_from == cap.valid_from
    assert snap.valid_to == cap.valid_to
    assert snap.new_loan_datum.to_cbor().hex() == fix["loan_out_datum"]
