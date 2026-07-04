"""Gated: dbsync resolves a ChangeCollateralSnapshot matching the capture; Ogmios evals.

The oracle witness (its signed reward redeemer) + the oracle feed / reference-script
UTxOs are injected from the capture -- they are an off-chain, time-bound dependency that
cannot be derived from the loan alone. The forward build pins the validity window to the
captured one (via ``EvalContext(last_block_slot=invalid_before)``) so the captured
witness's ms window still covers it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

load_dotenv()

_FIX = Path(__file__).parent / "fixtures"
_FILE = "change_collateral.json"

_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _gate() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not os.environ.get("DBSYNC_HOST"),
        reason="needs dbsync",
    )


def _utxo_entry(u: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {"ada": {"lovelace": int(u["lovelace"])}}
    for policy, asset_name, qty in u["assets"]:
        value.setdefault(policy, {})[asset_name] = int(qty)
    entry: dict[str, Any] = {
        "transaction": {"id": u["out_ref"][0]},
        "index": u["out_ref"][1],
        "address": u["address"],
        "value": value,
    }
    if u.get("datum"):
        entry["datum"] = u["datum"]
    if u.get("ref_script"):
        lang = _SCRIPT_LANG.get(u.get("ref_script_type") or "", "plutus:v3")
        entry["script"] = {"language": lang, "cbor": u["ref_script"]}
    return entry


def _additional_utxo(fix: dict[str, Any]) -> list[dict[str, Any]]:
    return [_utxo_entry(u) for u in fix["inputs"] + fix["ref_inputs"]]


def _target_collateral(fix: dict[str, Any], snapshot: object) -> int:
    loan_out = fix["outputs"][0]
    return next(
        int(qty)
        for policy, _name, qty in loan_out["assets"]
        if policy != snapshot.loan_policy
    )


def _from_backend_snapshot(backend, cap):  # noqa: ANN001, ANN201
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        ChangeCollateralSnapshot,
    )

    return ChangeCollateralSnapshot.from_backend(
        backend,
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


@_gate()
def test_change_collateral_from_backend_resolves_capture_fields() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        ChangeCollateralSnapshot,
    )

    fix = json.loads((_FIX / _FILE).read_text())
    cap = ChangeCollateralSnapshot.from_capture(fix)
    backend = get_backend()
    snap = _from_backend_snapshot(backend, cap)

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


@_gate()
def test_change_collateral_from_backend_builds_and_evaluates() -> None:
    from pycardano import TransactionBuilder

    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.change_collateral import (
        build_change_collateral,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        ChangeCollateralSnapshot,
    )
    from charli3_dendrite.lending.transactions.infra import (
        EvalContext,
        assemble_unsigned,
        evaluate_tx_cbor,
    )

    if not os.environ.get("OGMIOS_HOST"):
        pytest.skip("needs ogmios")

    fix = json.loads((_FIX / _FILE).read_text())
    cap = ChangeCollateralSnapshot.from_capture(fix)
    backend = get_backend()
    snap = _from_backend_snapshot(backend, cap)

    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_change_collateral(
        tx_builder,
        snapshot=snap,
        target_collateral=_target_collateral(fix, snap),
    )
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    # loan Spend + loan-policy Withdraw + change-collateral Withdraw + oracle Withdraw.
    assert purposes == ["spend", "withdraw", "withdraw", "withdraw"]
