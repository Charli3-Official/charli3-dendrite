"""Offline byte-exact: a forward-built request-fill matches the capture."""

from __future__ import annotations

import json
from pathlib import Path

from charli3_dendrite.lending.fluidtokens.constants import BORROWER_BOND_POLICY
from charli3_dendrite.lending.fluidtokens.constants import LOAN_POLICY
from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.lend import build_lend
from charli3_dendrite.lending.fluidtokens.transactions.lend import loan_nft_name
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from pycardano import Transaction
from pycardano import TransactionBuilder

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward"}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


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


def _build(fix: dict) -> Transaction:
    snapshot = LendSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_lend(tx_builder, snapshot=snapshot)
    return Transaction.from_cbor(assemble_unsigned(tx_builder))


def test_lend_redeemers_are_byte_exact() -> None:
    fix = _fixture("lend.json")
    synth = _synth_redeemers(_build(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_lend_derives_on_chain_loan_nft_name() -> None:
    fix = _fixture("lend.json")
    snapshot = LendSnapshot.from_capture(fix)
    minted = next(n for p, n, q in fix["mints"] if p == LOAN_POLICY and q == 1)
    assert loan_nft_name(snapshot.request.out_ref).hex() == minted
    assert snapshot.loan_id.hex() == minted


def test_lend_loan_output_datum_is_byte_exact() -> None:
    fix = _fixture("lend.json")
    tx = _build(fix)
    loan_out = next(
        o
        for o in tx.transaction_body.outputs
        if any(bytes(p).hex() == LOAN_POLICY for p in o.amount.multi_asset)
    )
    assert loan_out.datum.to_cbor().hex() == fix["loan_out_datum"]


def test_lend_borrower_output_is_first_and_carries_request_ref() -> None:
    fix = _fixture("lend.json")
    tx = _build(fix)
    borrower_out = tx.transaction_body.outputs[0]
    assert any(
        bytes(p).hex() == BORROWER_BOND_POLICY for p in borrower_out.amount.multi_asset
    )
    captured_borrower = next(
        o
        for o in fix["outputs"]
        if any(a[0] == BORROWER_BOND_POLICY for a in o["assets"])
    )
    assert borrower_out.datum.to_cbor().hex() == captured_borrower["datum"]
