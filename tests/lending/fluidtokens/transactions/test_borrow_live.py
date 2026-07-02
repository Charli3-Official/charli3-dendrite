"""Gated: dbsync resolves a BorrowSnapshot matching the capture; Ogmios evaluates it.

The oracle witness (its signed reward redeemer) + the oracle feed / reference-script
UTxOs + the borrow protocol fee are injected from the capture -- they are off-chain,
time-bound dependencies that cannot be derived from the pool alone. The forward build
pins the validity window to the captured one (via
``EvalContext(last_block_slot=invalid_before)``) so the captured witness's ms window
still covers it.
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
_FILE = "borrow_pool.json"

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


def _from_backend_snapshot(backend, cap):  # noqa: ANN001, ANN201
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    return BorrowSnapshot.from_backend(
        backend,
        pool_utxo=cap.pool.out_ref,
        borrower_address=cap.borrower_address,
        principal_amount=cap.principal_amount,
        chosen_collateral_index=cap.chosen_collateral_index,
        oracle_reward_cbor=cap.oracle_reward_cbor,
        oracle_feed_outref=cap.oracle_feed.out_ref,
        oracle_script_ref_outref=cap.oracle_script_ref.out_ref,
        fee_lovelace=cap.fee_lovelace,
        collateral_amount=cap.collateral_amount,
        valid_from=cap.valid_from,
        valid_to=cap.valid_to,
        allow_spent=True,
        funding_outrefs=[u.out_ref for u in cap.funding],
        borrower_output_lovelace=cap.borrower_output_lovelace,
        config_outref=cap.config.out_ref,
        pool_spend_ref_outref=cap.pool_spend_script_ref.out_ref,
        pool_policy_ref_outref=cap.pool_policy_script_ref.out_ref,
        loan_policy_ref_outref=cap.loan_policy_script_ref.out_ref,
        lender_bond_policy_ref_outref=cap.lender_bond_policy_script_ref.out_ref,
        borrower_bond_policy_ref_outref=cap.borrower_bond_policy_script_ref.out_ref,
    )


@_gate()
def test_from_backend_resolves_capture_fields() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    backend = get_backend()
    snap = _from_backend_snapshot(backend, cap)

    assert snap.pool == cap.pool
    assert snap.config == cap.config
    assert snap.oracle_feed == cap.oracle_feed
    assert snap.pool_spend_script_ref == cap.pool_spend_script_ref
    assert snap.pool_policy_script_ref == cap.pool_policy_script_ref
    assert snap.loan_policy_script_ref == cap.loan_policy_script_ref
    assert snap.lender_bond_policy_script_ref == cap.lender_bond_policy_script_ref
    assert snap.borrower_bond_policy_script_ref == cap.borrower_bond_policy_script_ref
    assert snap.oracle_script_ref == cap.oracle_script_ref
    assert snap.oracle_reward_cbor == cap.oracle_reward_cbor
    assert snap.loan_id == cap.loan_id
    assert snap.pool_id == cap.pool_id
    assert snap.loan_address == cap.loan_address
    assert snap.collateral_unit == cap.collateral_unit
    assert snap.collateral_amount == cap.collateral_amount
    assert snap.pool_continuation_lovelace == cap.pool_continuation_lovelace
    assert snap.valid_from == cap.valid_from
    assert snap.valid_to == cap.valid_to
    assert snap.lender_bond_out.address == cap.lender_bond_out.address
    assert snap.lender_bond_out.datum == cap.lender_bond_out.datum
    assert snap.lender_bond_out.assets == cap.lender_bond_out.assets
    assert [u.out_ref for u in snap.funding] == [u.out_ref for u in cap.funding]


@_gate()
def test_from_backend_builds_and_evaluates() -> None:
    from pycardano import TransactionBuilder

    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.borrow import build_borrow
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot
    from charli3_dendrite.lending.transactions.infra import (
        EvalContext,
        assemble_unsigned,
        evaluate_tx_cbor,
    )

    if not os.environ.get("OGMIOS_HOST"):
        pytest.skip("needs ogmios")

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    backend = get_backend()
    snap = _from_backend_snapshot(backend, cap)

    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_borrow(tx_builder, snapshot=snap)
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    # pool Spend + 3 mints + pool Withdraw (Borrow) + oracle Withdraw.
    assert purposes == ["mint", "mint", "mint", "spend", "withdraw", "withdraw"]
