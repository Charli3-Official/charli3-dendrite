"""Offline byte-exact: a forward-built pool-origin borrow matches the capture.

`build_borrow` synthesizes the pool reward (``Borrow``) redeemer (with the borrower
Plutus address, the lender-output / oracle / config indices, and the borrowed principal),
the loan-mint origin redeemer (pointing at the pool reward's position in the canonical
redeemer ordering), the borrower-/lender-bond mint redeemers (the spent pool out-ref),
and the continuing-loan :class:`LoanDatum` (terms inherited from the pool, ``origin_id``
= ``b"POOL"`` + the pool id, ``lend_date`` = the validity upper bound). This test
forward-builds against a captured real borrow and asserts every synthesized redeemer and
the synthesized loan datum are byte-identical to the on-chain ones -- no Ogmios required.
"""

from __future__ import annotations

import json
from pathlib import Path

from pycardano import Transaction
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions.borrow import build_borrow
from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FILE = "borrow_pool.json"
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward"}


def _fixture() -> dict:
    return json.loads((FIXTURES_DIR / FILE).read_text())


def _build(fix: dict) -> Transaction:
    snapshot = BorrowSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_borrow(tx_builder, snapshot=snapshot)
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


def test_borrow_redeemers_are_byte_exact() -> None:
    """Every forward-built redeemer matches the captured on-chain redeemer."""
    fix = _fixture()
    synth = _synth_redeemers(_build(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_borrow_synthesized_loan_datum_is_byte_exact() -> None:
    """The continuing-loan datum (output 1) matches the on-chain loan datum."""
    fix = _fixture()
    tx = _build(fix)
    assert (
        tx.transaction_body.outputs[1].datum.to_cbor().hex()
        == fix["outputs"][1]["datum"]
    )


def test_borrow_pool_and_lender_bond_datums_carried_verbatim() -> None:
    """The continuing pool (output 0) + lender-bond (output 2) datums are preserved."""
    fix = _fixture()
    tx = _build(fix)
    assert (
        tx.transaction_body.outputs[0].datum.to_cbor().hex()
        == fix["outputs"][0]["datum"]
    )
    assert (
        tx.transaction_body.outputs[2].datum.to_cbor().hex()
        == fix["outputs"][2]["datum"]
    )
