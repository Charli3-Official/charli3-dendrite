"""Offline byte-exact: forward-built pool create / cancel match captures."""

from __future__ import annotations

import json
from pathlib import Path

from charli3_dendrite.lending.fluidtokens.constants import POOL_POLICY
from charli3_dendrite.lending.fluidtokens.transactions.context import CancelPoolSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import CreatePoolSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.pool import build_cancel_pool
from charli3_dendrite.lending.fluidtokens.transactions.pool import build_create_pool
from charli3_dendrite.lending.fluidtokens.transactions.pool import pool_nft_name
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


def _build_create(fix: dict) -> Transaction:
    snapshot = CreatePoolSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["block_time"]))
    build_create_pool(tx_builder, snapshot=snapshot)
    return Transaction.from_cbor(assemble_unsigned(tx_builder))


def _build_cancel(fix: dict) -> Transaction:
    snapshot = CancelPoolSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_cancel_pool(tx_builder, snapshot=snapshot)
    return Transaction.from_cbor(assemble_unsigned(tx_builder))


def test_create_pool_redeemer_is_byte_exact() -> None:
    fix = _fixture("pool_create.json")
    synth = _synth_redeemers(_build_create(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_create_pool_derives_on_chain_nft_name() -> None:
    fix = _fixture("pool_create.json")
    snapshot = CreatePoolSnapshot.from_capture(fix)
    minted = next(n for p, n, _ in fix["mints"] if p == snapshot.pool_policy)
    assert pool_nft_name(snapshot.input_ref).hex() == minted


def test_create_pool_output_datum_carried_verbatim() -> None:
    fix = _fixture("pool_create.json")
    tx = _build_create(fix)
    pool_out = next(
        u
        for u in fix["outputs"]
        if u.get("datum") and any(a[0] == POOL_POLICY for a in u["assets"])
    )
    assert tx.transaction_body.outputs[0].datum.to_cbor().hex() == pool_out["datum"]


def test_cancel_pool_redeemers_are_byte_exact() -> None:
    fix = _fixture("pool_cancel.json")
    synth = _synth_redeemers(_build_cancel(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_cancel_pool_requires_lender_signature() -> None:
    fix = _fixture("pool_cancel.json")
    tx = _build_cancel(fix)
    snapshot = CancelPoolSnapshot.from_capture(fix)
    required = {bytes(s).hex() for s in (tx.transaction_body.required_signers or [])}
    assert snapshot.lender_pkh.hex() in required
