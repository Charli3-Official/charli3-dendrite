"""Shared helpers for the V4 builder tests: fixtures, forward builds and Ogmios."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pycardano import Address
from pycardano import RawCBOR
from pycardano import Transaction
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_value
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor

FIXTURES = Path(__file__).parent / "fixtures"
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward"}

needs_ogmios = pytest.mark.skipif(
    not os.environ.get("OGMIOS_HOST"),
    reason="needs OGMIOS_HOST",
)
needs_dbsync = pytest.mark.skipif(
    not (os.environ.get("DBSYNC_HOST") and os.environ.get("OGMIOS_HOST")),
    reason="needs DBSYNC_* and OGMIOS_HOST",
)


def fixture(name: str) -> dict[str, Any]:
    """A captured transaction fixture."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def reference_script(label: str) -> Utxo:
    """A captured reference-script UTxO from ``reference_scripts.json``."""
    return utxo_from_dict(fixture("reference_scripts")[label])


def fixture_utxos(fix: dict[str, Any]) -> list[Utxo]:
    """A capture's spent inputs and reference inputs."""
    return [utxo_from_dict(u) for u in fix["inputs"] + fix["ref_inputs"]]


def at_minimum_ada(utxo: Utxo) -> Utxo:
    """``utxo`` holding exactly the minimum ADA of its own output."""
    output = TransactionOutput(
        Address.decode(utxo.address),
        utxo_value(utxo.lovelace, utxo.assets),
        datum=RawCBOR(bytes.fromhex(utxo.datum)) if utxo.datum else None,
    )
    return replace(utxo, lovelace=min_ada(output))


@dataclass
class Built:
    """An assembled unsigned transaction: its CBOR as built, and decoded."""

    cbor: str

    @property
    def tx(self) -> Transaction:
        """The decoded transaction (for inspection only; evaluate :attr:`cbor`)."""
        return Transaction.from_cbor(self.cbor)


def build(
    contribute: Callable[..., None],
    snapshot: object,
    *,
    slot: int,
) -> Built:
    """Forward-build ``snapshot`` into an unsigned transaction."""
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=slot))
    contribute(tx_builder, snapshot=snapshot)
    return Built(assemble_unsigned(tx_builder))


def redeemers(tx: Transaction) -> set[tuple[str, int, str]]:
    """``(purpose, index, CBOR hex)`` of every redeemer of ``tx``."""
    witness = tx.transaction_witness_set.redeemer
    items = (
        witness.items()
        if hasattr(witness, "items")
        else [(r.key, r.value) for r in witness]
    )
    out = set()
    for key, value in items:
        tag = key.tag.value if hasattr(key.tag, "value") else int(key.tag)
        out.add((_PURPOSE[int(tag)], int(key.index), value.data.to_cbor().hex()))
    return out


def captured_redeemers(fix: dict[str, Any]) -> set[tuple[str, int, str]]:
    """``(purpose, index, CBOR hex)`` of every redeemer of a capture."""
    return {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}


def output_view(
    output: Any,
) -> tuple[str, int, set[tuple[str, int]], str | None]:  # noqa: ANN401
    """``(address, lovelace, {(unit, qty)}, datum hex)`` of a built output."""
    assets = {
        (bytes(policy).hex() + name.payload.hex(), qty)
        for policy, names in output.amount.multi_asset.items()
        for name, qty in names.items()
    }
    datum = output.datum.to_cbor().hex() if output.datum is not None else None
    return str(output.address), output.amount.coin, assets, datum


def captured_output_view(output: dict[str, Any]) -> tuple[str, int, set, str | None]:
    """:func:`output_view` of a captured output."""
    assets = {(p + n, int(q)) for p, n, q in output["assets"]}
    return output["address"], int(output["lovelace"]), assets, output["datum"]


def evaluate(built: Built, utxos: Iterable[Utxo]) -> list[str]:
    """Ogmios-evaluate ``built`` with ``utxos`` supplied; the sorted redeemer purposes.

    Every script must succeed with a positive budget.
    """
    entries: dict[tuple[str, int] | None, dict[str, Any]] = {}
    for utxo in utxos:
        entries.setdefault(utxo.out_ref, ogmios_entry(utxo))
    budgets = evaluate_tx_cbor(built.cbor, list(entries.values()))
    for budget in budgets:
        assert budget["budget"]["memory"] > 0
        assert budget["budget"]["cpu"] > 0
    return sorted(b["validator"]["purpose"] for b in budgets)
