"""Offline byte-exact: a forward-built change-collateral matches the captured tx.

`build_change_collateral` synthesizes the loan-policy reward, the change-collateral
action reward (with the loan input index / new collateral amount / loan id / oracle-feed
reference index), and replays the signed oracle reward redeemer; it re-creates the loan
output with the new collateral amount and the unchanged datum. This test forward-builds
against a captured real change-collateral and asserts every synthesized redeemer is
byte-identical to the on-chain one and that the loan output datum is unchanged -- no
Ogmios required.
"""
from __future__ import annotations

import json
from pathlib import Path

from pycardano import Transaction
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions.change_collateral import (
    build_change_collateral,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FILE = "change_collateral.json"
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward"}


def _fixture() -> dict:
    return json.loads((FIXTURES_DIR / FILE).read_text())


def _target_collateral(fix: dict, snapshot: ChangeCollateralSnapshot) -> int:
    loan_out = fix["outputs"][0]
    return next(
        int(qty)
        for policy, _name, qty in loan_out["assets"]
        if policy != snapshot.loan_policy
    )


def _build(fix: dict) -> Transaction:
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_change_collateral(
        tx_builder,
        snapshot=snapshot,
        target_collateral=_target_collateral(fix, snapshot),
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


def test_change_collateral_redeemers_are_byte_exact() -> None:
    """Every forward-built redeemer matches the captured on-chain redeemer."""
    fix = _fixture()
    synth = _synth_redeemers(_build(fix))
    captured = {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}
    assert synth == captured


def test_change_collateral_loan_output_datum_is_unchanged() -> None:
    """The continuing loan output reproduces the input loan datum byte-exact."""
    fix = _fixture()
    tx = _build(fix)
    assert tx.transaction_body.outputs[0].datum.to_cbor().hex() == fix["loan_out_datum"]
