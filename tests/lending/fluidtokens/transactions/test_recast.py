"""Offline byte-exact: a forward-built recast matches the captured transaction.

`build_recast` synthesizes the loan-policy reward, the recast action reward (with the
loan input / lender-bond reference / bond-return + lender output indices, the paid
amount, and the loan id), the recast-receipt datum on the lender output, and the updated
continuing-loan datum (``done_recasts`` + 1, the new principal, the new lend date). This
test forward-builds against a captured real recast and asserts every synthesized redeemer
and both synthesized datums are byte-identical to the on-chain ones -- no Ogmios required.
"""
from __future__ import annotations

import json
from pathlib import Path

from pycardano import Transaction
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions.context import RecastSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.recast import build_recast
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FILE = "recast.json"
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward"}


def _fixture() -> dict:
    return json.loads((FIXTURES_DIR / FILE).read_text())


def _build(fix: dict) -> Transaction:
    snapshot = RecastSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_recast(tx_builder, snapshot=snapshot)
    return Transaction.from_cbor(assemble_unsigned(tx_builder))


def _synth_redeemers(tx: Transaction) -> set[tuple[str, int, str]]:
    redeemers = tx.transaction_witness_set.redeemer
    items = (
        redeemers.items()
        if hasattr(redeemers, "items")
        else [(r.key, r.value) for r in redeemers]
    )
    out: set[tuple[str, int, str]] = set()
    for key, value in items:
        tag = key.tag.value if hasattr(key.tag, "value") else int(key.tag)
        out.add((_PURPOSE[int(tag)], int(key.index), value.data.to_cbor().hex()))
    return out


def test_recast_redeemers_are_byte_exact() -> None:
    """Every forward-built redeemer matches the captured on-chain redeemer."""
    fix = _fixture()
    synth = _synth_redeemers(_build(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_recast_synthesized_datums_are_byte_exact() -> None:
    """The lender receipt + the updated continuing-loan datum match the on-chain ones."""
    fix = _fixture()
    tx = _build(fix)
    assert (
        tx.transaction_body.outputs[0].datum.to_cbor().hex()
        == fix["outputs"][0]["datum"]
    )
    assert tx.transaction_body.outputs[2].datum.to_cbor().hex() == fix["loan_out_datum"]
