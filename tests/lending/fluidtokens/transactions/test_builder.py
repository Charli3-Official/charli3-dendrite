"""Offline: the FluidTokens tx-builder dispatches each action to its contributor.

`FluidTokensTxBuilder.contribute` is the registry-facing seam over the per-action
``build_*`` functions. These tests assert the supported-action set, that each action
dispatches to the right contributor (the captured redeemers land byte-exact in the
assembled tx -- identical to calling the ``build_*`` directly), and that the unwired
live-resolution / unsupported-action paths fail loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from charli3_dendrite.lending.fluidtokens.transactions.builder import (
    FluidTokensTxBuilder,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import CancelPoolSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    CancelRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import CreatePoolSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    CreateRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import RecastSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import RepaySnapshot
from charli3_dendrite.lending.transactions.base import ActionParams
from charli3_dendrite.lending.transactions.base import LendingAction
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from pycardano import Transaction
from pycardano import TransactionBuilder

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# action -> (fixture file, snapshot class) for the dispatch round-trip.
_CASES = {
    LendingAction.BORROW: ("borrow_pool.json", BorrowSnapshot),
    LendingAction.REPAY: ("repay_full.json", RepaySnapshot),
    LendingAction.MODIFY_COLLATERAL: (
        "change_collateral.json",
        ChangeCollateralSnapshot,
    ),
    LendingAction.RECAST: ("recast.json", RecastSnapshot),
    LendingAction.REQUEST_CREATE: ("create_request.json", CreateRequestSnapshot),
    LendingAction.REQUEST_CANCEL: ("cancel_request.json", CancelRequestSnapshot),
    LendingAction.POOL_CREATE: ("pool_create.json", CreatePoolSnapshot),
    LendingAction.POOL_CANCEL: ("pool_cancel.json", CancelPoolSnapshot),
}
_PARAMS = ActionParams(actor_address="unused")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _slot(fix: dict) -> int:
    if fix.get("invalid_before"):
        return int(fix["invalid_before"])
    return int(fix["block_time"])


def _params_for(action: LendingAction, fix: dict, snapshot: object) -> ActionParams:
    """The seam params an action consumes; REPAY/MODIFY thread one magnitude."""
    if action == LendingAction.REPAY:
        return ActionParams(
            actor_address="unused",
            amount=int(fix["outputs"][0]["lovelace"]),
        )
    if action == LendingAction.MODIFY_COLLATERAL:
        loan_out = fix["outputs"][0]
        target = next(
            int(qty)
            for policy, _name, qty in loan_out["assets"]
            if policy != snapshot.loan_policy
        )
        return ActionParams(actor_address="unused", amount=target)
    return _PARAMS


def test_supported_actions_are_the_borrower_and_pool_actions():
    assert FluidTokensTxBuilder.supported_actions() == {
        LendingAction.BORROW,
        LendingAction.REPAY,
        LendingAction.MODIFY_COLLATERAL,
        LendingAction.RECAST,
        LendingAction.REQUEST_CREATE,
        LendingAction.REQUEST_CANCEL,
        LendingAction.POOL_CREATE,
        LendingAction.POOL_CANCEL,
    }
    assert FluidTokensTxBuilder.protocol() == "FluidTokens"


@pytest.mark.parametrize(("action", "case"), list(_CASES.items()))
def test_contribute_dispatches_to_the_right_contributor(
    action: LendingAction,
    case: tuple[str, type],
) -> None:
    """Each action wires its contributor: every captured redeemer lands byte-exact."""
    fixture_name, snapshot_cls = case
    fix = _fixture(fixture_name)
    snapshot = snapshot_cls.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=_slot(fix)))
    FluidTokensTxBuilder().contribute(
        action,
        tx_builder,
        snapshot=snapshot,
        params=_params_for(action, fix, snapshot),
    )
    cbor = assemble_unsigned(tx_builder)
    Transaction.from_cbor(cbor)  # assembles into a structurally-valid tx
    for redeemer in fix["redeemers"]:
        assert redeemer["cbor"] in cbor


def test_contribute_rejects_mismatched_snapshot_type():
    fix = _fixture("create_request.json")
    snapshot = CreateRequestSnapshot.from_capture(fix)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=_slot(fix)))
    with pytest.raises(TypeError, match="expects BorrowSnapshot"):
        FluidTokensTxBuilder().contribute(
            LendingAction.BORROW,
            tx_builder,
            snapshot=snapshot,
            params=_PARAMS,
        )


def test_build_and_evaluate_rejects_unsupported_action():
    with pytest.raises(ValueError, match="does not support action 'deposit'"):
        FluidTokensTxBuilder().build_and_evaluate(
            backend=None,
            market_name="x",
            action=LendingAction.DEPOSIT,
            params=_PARAMS,
        )


def test_resolve_snapshot_is_not_wired_yet():
    with pytest.raises(NotImplementedError, match="from_capture"):
        FluidTokensTxBuilder().resolve_snapshot(
            None,
            market_name="x",
            action=LendingAction.BORROW,
            params=_PARAMS,
        )
