"""Offline byte-exact: forward-built create / cancel borrow-requests match captures.

`build_create_request` derives the request NFT asset name from the chosen input
out-ref, mints it, and locks it + the collateral + the (verbatim) request datum at the
request spend address. `build_cancel_request` spends the request UTxO, burns the
request NFT, and drives the request-policy ``Cancel`` reward (with the borrower required
signer). These tests forward-build against captured real create / cancel and assert
every synthesized redeemer (and the create request output datum) is byte-identical to
the on-chain one -- no Ogmios required.
"""

from __future__ import annotations

import json
from pathlib import Path

from charli3_dendrite.lending.fluidtokens.constants import REQUEST_POLICY
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    CancelRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    CreateRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.request import (
    build_cancel_request,
)
from charli3_dendrite.lending.fluidtokens.transactions.request import (
    build_create_request,
)
from charli3_dendrite.lending.fluidtokens.transactions.request import request_nft_name
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
    snapshot = CreateRequestSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["block_time"]))
    build_create_request(tx_builder, snapshot=snapshot)
    return Transaction.from_cbor(assemble_unsigned(tx_builder))


def _build_cancel(fix: dict) -> Transaction:
    snapshot = CancelRequestSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_cancel_request(tx_builder, snapshot=snapshot)
    return Transaction.from_cbor(assemble_unsigned(tx_builder))


def test_create_request_redeemer_is_byte_exact() -> None:
    """The forward-built request-mint redeemer matches the captured one."""
    fix = _fixture("create_request.json")
    synth = _synth_redeemers(_build_create(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_create_request_derives_on_chain_nft_name() -> None:
    """The derived request NFT name matches the minted on-chain asset name."""
    fix = _fixture("create_request.json")
    snapshot = CreateRequestSnapshot.from_capture(fix)
    minted = next(n for p, n, _ in fix["mints"] if p == snapshot.request_policy)
    assert request_nft_name(snapshot.input_ref).hex() == minted


def test_create_request_output_datum_carried_verbatim() -> None:
    """The request output (datum + request NFT) reproduces the on-chain one."""
    fix = _fixture("create_request.json")
    tx = _build_create(fix)
    request_out = next(
        u
        for u in fix["outputs"]
        if u.get("datum") and any(a[0] == REQUEST_POLICY for a in u["assets"])
    )
    assert tx.transaction_body.outputs[0].datum.to_cbor().hex() == request_out["datum"]


def test_cancel_request_redeemers_are_byte_exact() -> None:
    """Every forward-built cancel redeemer matches the captured on-chain redeemer."""
    fix = _fixture("cancel_request.json")
    synth = _synth_redeemers(_build_cancel(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_cancel_request_requires_borrower_signature() -> None:
    """The borrower's auth key hash is set as a required signer."""
    fix = _fixture("cancel_request.json")
    tx = _build_cancel(fix)
    snapshot = CancelRequestSnapshot.from_capture(fix)
    required = {bytes(s).hex() for s in (tx.transaction_body.required_signers or [])}
    assert snapshot.borrower_pkh.hex() in required
