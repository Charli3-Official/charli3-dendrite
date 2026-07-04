"""Gated end-to-end: a forward-built pool-origin borrow evaluates on Ogmios.

The decisive correctness proof for the borrow builder. Self-contained: it reconstructs a
`BorrowSnapshot` from a captured real borrow, forward-builds a brand-new (unsigned,
unsubmitted) borrow against the captured spent inputs, and asks the real Ogmios to
execute every Plutus script via ``evaluateTransaction`` -- feeding the captured spent
inputs + reference UTxOs back through ``additionalUtxo``.

A pool-origin borrow returns six positive execution budgets: the pool ``Spend``, the
three NFT mints (loan + borrower bond + lender bond), the pool reward ``Withdraw``
(``Borrow``), and the (replayed, signed) oracle ``Withdraw``. The validity window is
pinned to the captured transaction's so the validator's time checks and the replayed
oracle signature match. Nothing is signed or submitted.
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

from charli3_dendrite.lending.fluidtokens.transactions.borrow import (  # noqa: E402
    build_borrow,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    BorrowSnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FILE = "borrow_pool.json"

_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _gate() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (os.environ.get("OGMIOS_HOST") and (FIXTURES_DIR / FILE).exists()),
        reason="needs ogmios + the captured borrow fixture",
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


@_gate()
def test_borrow_evaluates_on_ogmios() -> None:
    fix = json.loads((FIXTURES_DIR / FILE).read_text())
    snapshot = BorrowSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_borrow(tx_builder, snapshot=snapshot)
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    # pool Spend + 3 mints + pool Withdraw (Borrow) + oracle Withdraw.
    assert purposes == ["mint", "mint", "mint", "spend", "withdraw", "withdraw"]
