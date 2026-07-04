"""Gated end-to-end: a forward-built FluidTokens full repay evaluates on Ogmios.

The decisive correctness proof for the repay builder. It is self-contained: rather than
resolving live loan state (the captured loan is already closed/spent), it reconstructs a
`RepaySnapshot` from a captured real on-chain repay, forward-builds a brand-new
(unsigned, unsubmitted) repay against the captured spent inputs, and asks the real Ogmios
to execute every Plutus script via ``evaluateTransaction`` -- feeding the captured spent
inputs + reference UTxOs (datums + reference scripts) back through ``additionalUtxo``.

A full repay returns four positive execution budgets: the loan ``Spend``, the loan-NFT
``Mint`` burn, the loan-policy reward twin ``Withdraw``, and the repay-action
``Withdraw``. The validity window is pinned to the captured transaction's so the
validator's time checks match. Nothing is signed or submitted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

load_dotenv()
from pycardano import TransactionBuilder  # noqa: E402

from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    RepaySnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.repay import (  # noqa: E402
    build_repay,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import (  # noqa: E402
    assemble_unsigned,
)
from charli3_dendrite.lending.transactions.infra import (  # noqa: E402
    evaluate_tx_cbor,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FULL = "repay_full.json"

_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _gate(name: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (os.environ.get("OGMIOS_HOST") and (FIXTURES_DIR / name).exists()),
        reason="needs ogmios + the captured repay fixture",
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


@_gate(FULL)
def test_full_repay_evaluates_on_ogmios() -> None:
    fix = json.loads((FIXTURES_DIR / FULL).read_text())
    snapshot = RepaySnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_repay(
        tx_builder,
        snapshot=snapshot,
        lender_lovelace=int(fix["outputs"][0]["lovelace"]),
    )
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    # loan Spend + loan Mint burn + loan-policy Withdraw + repay-action Withdraw.
    assert purposes == ["mint", "spend", "withdraw", "withdraw"]
