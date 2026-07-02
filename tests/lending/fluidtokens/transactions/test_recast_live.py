"""Gated: dbsync live-resolution + Ogmios e2e for ``RecastSnapshot.from_backend``.

Two proofs that the live recast resolver produces a faithful, buildable snapshot:

* dbsync live resolution reconstructs the captured recast (the recapitalized
  principal, the reset lend date, the lender / fee payout, the validity window, and
  the byte-exact continuing-loan datum).
* A transaction forward-built from the LIVE-resolved snapshot evaluates on the real
  Ogmios, returning three positive budgets -- the loan ``spend`` plus the loan-policy
  reward twin and the recast-action reward (Ogmios reports both reward withdrawals
  under the ``withdraw`` purpose). Nothing is signed or submitted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

load_dotenv()
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    RecastSnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402
from pycardano import TransactionBuilder  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _dbsync_gate() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not os.environ.get("DBSYNC_HOST"),
        reason="needs dbsync",
    )


def _dbsync_ogmios_gate(file: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (
            os.environ.get("DBSYNC_HOST")
            and os.environ.get("OGMIOS_HOST")
            and (FIXTURES_DIR / file).exists()
        ),
        reason="needs dbsync + ogmios + the captured recast fixture",
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


def _resolve_snapshot(fix: dict[str, Any]) -> RecastSnapshot:
    from charli3_dendrite.backend import get_backend

    cap = RecastSnapshot.from_capture(fix)
    backend = get_backend()
    return RecastSnapshot.from_backend(
        backend,
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


@_dbsync_gate()
def test_recast_from_backend_resolves_capture_fields() -> None:
    fix = json.loads((FIXTURES_DIR / "recast.json").read_text())
    cap = RecastSnapshot.from_capture(fix)
    snap = _resolve_snapshot(fix)
    assert snap.new_principal_amount == cap.new_principal_amount
    assert snap.new_lend_date == cap.new_lend_date
    assert snap.lender_address == cap.lender_address
    assert snap.fee_address == cap.fee_address
    assert snap.fee_lovelace == cap.fee_lovelace
    assert snap.valid_from == cap.valid_from
    assert snap.valid_to == cap.valid_to
    assert snap.new_loan_datum.to_cbor().hex() == fix["loan_out_datum"]


@_dbsync_ogmios_gate("recast.json")
def test_recast_from_backend_evaluates_on_ogmios() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.recast import build_recast

    fix = json.loads((FIXTURES_DIR / "recast.json").read_text())
    snap = _resolve_snapshot(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_recast(tx_builder, snapshot=snap)
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    # loan spend + loan-policy reward twin + recast-action reward -- Ogmios
    # reports both reward withdrawals under the "withdraw" purpose.
    assert purposes == ["spend", "withdraw", "withdraw"]
