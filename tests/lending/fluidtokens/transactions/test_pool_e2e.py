"""Gated end-to-end: forward-built pool create / cancel evaluate on Ogmios."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

load_dotenv()
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    CancelPoolSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    CreatePoolSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.pool import (  # noqa: E402
    build_cancel_pool,
)
from charli3_dendrite.lending.fluidtokens.transactions.pool import (  # noqa: E402
    build_create_pool,
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


def _gate(file: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (os.environ.get("OGMIOS_HOST") and (FIXTURES_DIR / file).exists()),
        reason="needs ogmios + the captured pool fixture",
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


@_gate("pool_create.json")
def test_create_pool_evaluates_on_ogmios() -> None:
    fix = json.loads((FIXTURES_DIR / "pool_create.json").read_text())
    snapshot = CreatePoolSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["block_time"]))
    build_create_pool(tx_builder, snapshot=snapshot)
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    assert sorted(e["validator"]["purpose"] for e in budgets) == ["mint"]


@_gate("pool_cancel.json")
def test_cancel_pool_evaluates_on_ogmios() -> None:
    fix = json.loads((FIXTURES_DIR / "pool_cancel.json").read_text())
    snapshot = CancelPoolSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_cancel_pool(tx_builder, snapshot=snapshot)
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    # pool-NFT burn (mint) + pool spend + pool-policy Cancel withdraw.
    assert sorted(e["validator"]["purpose"] for e in budgets) == [
        "mint",
        "spend",
        "withdraw",
    ]
