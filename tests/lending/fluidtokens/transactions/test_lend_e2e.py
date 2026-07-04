"""Gated end-to-end: a forward-built request-fill evaluates on Ogmios."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

load_dotenv()
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    LendSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    _as_utxo,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (  # noqa: E402
    ogmios_entry,
)
from charli3_dendrite.lending.fluidtokens.transactions.lend import (  # noqa: E402
    build_lend,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402
from pycardano import TransactionBuilder  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _gate(file: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (os.environ.get("OGMIOS_HOST") and (FIXTURES_DIR / file).exists()),
        reason="needs ogmios + the captured lend fixture",
    )


def _additional_utxo(fix: dict[str, Any]) -> list[dict[str, Any]]:
    return [ogmios_entry(_as_utxo(u)) for u in fix["inputs"] + fix["ref_inputs"]]


@_gate("lend.json")
def test_lend_evaluates_on_ogmios() -> None:
    fix = json.loads((FIXTURES_DIR / "lend.json").read_text())
    snapshot = LendSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_lend(tx_builder, snapshot=snapshot)
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    # request-NFT burn + 3 mints (mint) + request spend + request-policy Lend withdraw.
    assert sorted(e["validator"]["purpose"] for e in budgets) == [
        "mint",
        "mint",
        "mint",
        "mint",
        "spend",
        "withdraw",
    ]
