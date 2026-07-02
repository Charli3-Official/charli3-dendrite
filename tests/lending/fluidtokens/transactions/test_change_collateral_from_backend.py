"""Offline: ChangeCollateralSnapshot.from_backend rebuilds the captured change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIX = Path(__file__).parent / "fixtures"


def test_change_collateral_from_backend_matches_capture(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        ChangeCollateralSnapshot,
        _as_utxo,
    )

    fix = json.loads((_FIX / "change_collateral.json").read_text())
    cap = ChangeCollateralSnapshot.from_capture(fix)

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

    snap = ChangeCollateralSnapshot.from_backend(
        None,
        loan_utxo=cap.loan.out_ref,
        oracle_reward_cbor=cap.oracle_reward_cbor,
        oracle_feed_outref=cap.oracle_feed.out_ref,
        actor_address=cap.borrower_bond.address,
        allow_spent=True,
        config_outref=cap.config.out_ref,
        spend_ref_outref=cap.spend_script_ref.out_ref,
        loan_policy_ref_outref=cap.loan_policy_script_ref.out_ref,
        action_ref_outref=cap.action_script_ref.out_ref,
        oracle_script_ref_outref=cap.oracle_script_ref.out_ref,
        borrower_bond_outref=cap.borrower_bond.out_ref,
    )

    assert snap.loan == cap.loan
    assert snap.loan_id == cap.loan_id
    assert snap.borrower_bond == cap.borrower_bond
    assert snap.config == cap.config
    assert snap.oracle_feed == cap.oracle_feed
    assert snap.spend_script_ref == cap.spend_script_ref
    assert snap.loan_policy_script_ref == cap.loan_policy_script_ref
    assert snap.action_script_ref == cap.action_script_ref
    assert snap.oracle_script_ref == cap.oracle_script_ref
    assert snap.oracle_reward_cbor == cap.oracle_reward_cbor
    assert snap.loan_policy == cap.loan_policy
    assert snap.bond_policy == cap.bond_policy


def test_change_collateral_from_backend_rejects_malformed_witness(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        ChangeCollateralSnapshot,
        _as_utxo,
    )

    fix = json.loads((_FIX / "change_collateral.json").read_text())
    cap = ChangeCollateralSnapshot.from_capture(fix)
    by_ref = {
        tuple(u["out_ref"]): _as_utxo(u)
        for u in fix["inputs"] + fix["ref_inputs"]
        if u.get("out_ref")
    }
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_utxo_by_outref",
        lambda backend, h, i, *, allow_spent=False: by_ref[(h, i)],
    )

    with pytest.raises(ValueError, match="oracle reward"):
        ChangeCollateralSnapshot.from_backend(
            None,
            loan_utxo=cap.loan.out_ref,
            oracle_reward_cbor="deadbeef",
            oracle_feed_outref=cap.oracle_feed.out_ref,
            allow_spent=True,
            oracle_script_ref_outref=cap.oracle_script_ref.out_ref,
        )
