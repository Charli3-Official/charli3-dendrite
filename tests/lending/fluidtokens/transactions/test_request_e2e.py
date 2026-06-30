"""Gated end-to-end: forward-built create / cancel borrow-requests evaluate on Ogmios.

The decisive correctness proof for the request builders. Self-contained: it
reconstructs a :class:`CreateRequestSnapshot` / :class:`CancelRequestSnapshot` from a
captured real create / cancel, forward-builds a brand-new (unsigned, unsubmitted)
transaction against the captured spent inputs, and asks the real Ogmios to execute every
Plutus script via ``evaluateTransaction`` -- feeding the captured spent inputs +
reference UTxOs back through ``additionalUtxo``.

* Create returns one positive budget: the request-NFT mint.
* Cancel returns three positive budgets: the request-NFT burn (``mint``), the request
  ``spend``, and the request-policy ``Cancel`` ``withdraw`` -- the last authorized by
  the borrower required signer. Nothing is signed or submitted.
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
    CancelRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    CreateRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.request import (  # noqa: E402
    build_cancel_request,
)
from charli3_dendrite.lending.fluidtokens.transactions.request import (  # noqa: E402
    build_create_request,
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
        reason="needs ogmios + the captured request fixture",
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


@_gate("create_request.json")
def test_create_request_evaluates_on_ogmios() -> None:
    fix = json.loads((FIXTURES_DIR / "create_request.json").read_text())
    snapshot = CreateRequestSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["block_time"]))
    build_create_request(tx_builder, snapshot=snapshot)
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    assert purposes == ["mint"]


@_gate("cancel_request.json")
def test_cancel_request_evaluates_on_ogmios() -> None:
    fix = json.loads((FIXTURES_DIR / "cancel_request.json").read_text())
    snapshot = CancelRequestSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_cancel_request(tx_builder, snapshot=snapshot)
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    # request-NFT burn (mint) + request spend + request-policy Cancel withdraw.
    assert purposes == ["mint", "spend", "withdraw"]
