"""Offline byte-exact: a forward-built repay reproduces the captured redeemer set.

`build_repay` synthesizes the four repay redeemers (loan ``Spend``, loan-NFT ``Mint``
burn, the loan-policy reward twin ``Withdraw``, and the repay-action ``Withdraw``) and
their reference-input / output / input indices from the final canonical ordering. This
test forward-builds against a captured real repay and asserts every synthesized redeemer
is byte-identical to the on-chain one -- no Ogmios required.
"""
from __future__ import annotations

import json
from pathlib import Path

from pycardano import Transaction
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions.context import RepaySnapshot
from charli3_dendrite.lending.fluidtokens.transactions.repay import build_repay
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward"}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _build(fix: dict) -> Transaction:
    snapshot = RepaySnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_repay(
        tx_builder,
        snapshot=snapshot,
        lender_lovelace=int(fix["outputs"][0]["lovelace"]),
    )
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


def test_full_repay_redeemers_are_byte_exact() -> None:
    """Every forward-built repay redeemer matches the captured on-chain redeemer."""
    fix = _fixture("repay_full.json")
    synth = _synth_redeemers(_build(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_full_repay_has_four_outputs_lender_first() -> None:
    """The repay emits lender / bond-return / fee / collateral-release in that order."""
    fix = _fixture("repay_full.json")
    tx = _build(fix)
    assert len(tx.transaction_body.outputs) == 4
    # The lender output (index 0) carries the repayment-receipt inline datum.
    assert tx.transaction_body.outputs[0].datum is not None
