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
from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
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
    LendingAction.LEND: ("lend.json", LendSnapshot),
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
        LendingAction.LEND,
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


def test_resolve_snapshot_borrow_directs_to_from_backend():
    with pytest.raises(
        NotImplementedError,
        match="BorrowSnapshot.from_backend",
    ):
        FluidTokensTxBuilder().resolve_snapshot(
            None,
            market_name="x",
            action=LendingAction.BORROW,
            params=_PARAMS,
        )


def test_contribute_stashes_pool_funding_additional_utxo() -> None:
    # both pool actions must populate additional_utxo() so build_and_evaluate can feed
    # Ogmios the actor (funding) inputs it cannot resolve from its own ledger snapshot.
    for action, snap_cls, fixture, slot_key in [
        (
            LendingAction.POOL_CREATE,
            CreatePoolSnapshot,
            "pool_create.json",
            "block_time",
        ),
        (
            LendingAction.POOL_CANCEL,
            CancelPoolSnapshot,
            "pool_cancel.json",
            "invalid_before",
        ),
        (
            LendingAction.LEND,
            LendSnapshot,
            "lend.json",
            "invalid_before",
        ),
    ]:
        fix = _fixture(fixture)
        snapshot = snap_cls.from_capture(fix)
        tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix[slot_key]))
        FluidTokensTxBuilder().contribute(
            action,
            tx_builder,
            snapshot=snapshot,
            params=ActionParams(actor_address=""),
        )
        entries = snapshot.additional_utxo()
        assert len(entries) == len(snapshot.funding)
        assert all("transaction" in e and "value" in e for e in entries)


def test_resolve_snapshot_pool_cancel_dispatches_to_from_backend(monkeypatch) -> None:
    captured = {}

    def record(cls, backend, *, pool_utxo, lender_address):  # noqa: ANN001, ARG001
        captured["pool_utxo"] = pool_utxo
        captured["lender_address"] = lender_address
        return object()

    monkeypatch.setattr(
        CancelPoolSnapshot,
        "from_backend",
        classmethod(record),
    )
    FluidTokensTxBuilder().resolve_snapshot(
        object(),
        market_name="",
        action=LendingAction.POOL_CANCEL,
        params=ActionParams(actor_address="addr_lender", loan_utxo="abc123#2"),
    )
    assert captured["pool_utxo"] == ("abc123", 2)
    assert captured["lender_address"] == "addr_lender"


def test_resolve_snapshot_pool_cancel_requires_loan_utxo() -> None:
    with pytest.raises(ValueError, match="POOL_CANCEL requires params.loan_utxo"):
        FluidTokensTxBuilder().resolve_snapshot(
            object(),
            market_name="",
            action=LendingAction.POOL_CANCEL,
            params=ActionParams(actor_address="addr_lender"),
        )


def test_resolve_snapshot_pool_create_is_not_supported_via_params() -> None:
    with pytest.raises(NotImplementedError):
        FluidTokensTxBuilder().resolve_snapshot(
            object(),
            market_name="",
            action=LendingAction.POOL_CREATE,
            params=ActionParams(actor_address=""),
        )


def test_resolve_snapshot_lend_dispatches_to_from_backend(monkeypatch) -> None:
    captured = {}

    def record(
        cls,  # noqa: ANN001
        backend,  # noqa: ANN001, ARG001
        *,
        request_utxo,  # noqa: ANN001
        given_principal_amount,  # noqa: ANN001
        lender_address,  # noqa: ANN001
    ):
        captured["request_utxo"] = request_utxo
        captured["given_principal_amount"] = given_principal_amount
        captured["lender_address"] = lender_address
        return object()

    monkeypatch.setattr(LendSnapshot, "from_backend", classmethod(record))
    FluidTokensTxBuilder().resolve_snapshot(
        object(),
        market_name="",
        action=LendingAction.LEND,
        params=ActionParams(
            actor_address="addr_lender",
            loan_utxo="abc123#7",
            amount=5_000_000,
        ),
    )
    assert captured["request_utxo"] == ("abc123", 7)
    assert captured["given_principal_amount"] == 5_000_000
    assert captured["lender_address"] == "addr_lender"


def test_resolve_snapshot_lend_requires_loan_utxo() -> None:
    with pytest.raises(ValueError, match="LEND requires params.loan_utxo"):
        FluidTokensTxBuilder().resolve_snapshot(
            object(),
            market_name="",
            action=LendingAction.LEND,
            params=ActionParams(actor_address="addr_lender"),
        )


def test_resolve_snapshot_repay_dispatches_to_from_backend(monkeypatch) -> None:
    captured = {}

    def fake_from_backend(backend, **kwargs):  # noqa: ANN001, ANN003, ARG001
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        RepaySnapshot,
        "from_backend",
        classmethod(
            lambda cls, backend, **kw: fake_from_backend(backend, **kw),  # noqa: ARG005
        ),
    )
    FluidTokensTxBuilder().resolve_snapshot(
        backend=None,
        market_name="m",
        action=LendingAction.REPAY,
        params=ActionParams(actor_address="addr1x", loan_utxo="ab" * 32 + "#1"),
    )
    assert captured["loan_utxo"] == ("ab" * 32, 1)
    assert captured["actor_address"] == "addr1x"


def test_resolve_snapshot_repay_requires_loan_utxo() -> None:
    with pytest.raises(ValueError, match="REPAY requires params.loan_utxo"):
        FluidTokensTxBuilder().resolve_snapshot(
            backend=None,
            market_name="m",
            action=LendingAction.REPAY,
            params=ActionParams(actor_address="addr1x"),
        )


def test_resolve_snapshot_recast_dispatches_to_from_backend(monkeypatch) -> None:
    captured = {}

    def fake_from_backend(backend, **kwargs):  # noqa: ANN001, ANN003, ARG001
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        RecastSnapshot,
        "from_backend",
        classmethod(
            lambda cls, backend, **kw: fake_from_backend(backend, **kw),  # noqa: ARG005
        ),
    )
    FluidTokensTxBuilder().resolve_snapshot(
        backend=None,
        market_name="m",
        action=LendingAction.RECAST,
        params=ActionParams(
            actor_address="addrB",
            loan_utxo="aa" * 32 + "#0",
            amount=5_000_000,
        ),
    )
    assert captured["loan_utxo"] == ("aa" * 32, 0)
    assert captured["actor_address"] == "addrB"
    assert captured["amount_paid"] == 5_000_000


def test_resolve_snapshot_recast_requires_loan_utxo() -> None:
    with pytest.raises(ValueError, match="RECAST requires params.loan_utxo"):
        FluidTokensTxBuilder().resolve_snapshot(
            backend=None,
            market_name="m",
            action=LendingAction.RECAST,
            params=ActionParams(actor_address="addrB", amount=5_000_000),
        )


def test_resolve_snapshot_recast_requires_amount() -> None:
    with pytest.raises(ValueError, match="RECAST requires params.amount"):
        FluidTokensTxBuilder().resolve_snapshot(
            backend=None,
            market_name="m",
            action=LendingAction.RECAST,
            params=ActionParams(
                actor_address="addrB",
                loan_utxo="aa" * 32 + "#0",
                amount=None,
            ),
        )


def test_resolve_snapshot_cancel_request_dispatches(monkeypatch) -> None:
    captured = {}
    # from_backend lands in a later task; register it so the dispatch can be exercised.
    monkeypatch.setattr(
        CancelRequestSnapshot,
        "from_backend",
        classmethod(
            lambda cls, backend, **kw: captured.update(kw) or object(),  # noqa: ARG005
        ),
        raising=False,
    )
    FluidTokensTxBuilder().resolve_snapshot(
        backend=None,
        market_name="m",
        action=LendingAction.REQUEST_CANCEL,
        params=ActionParams(actor_address="addr1b", loan_utxo="cd" * 32 + "#0"),
    )
    assert captured["request_utxo"] == ("cd" * 32, 0)
    assert captured["borrower_address"] == "addr1b"


def test_resolve_snapshot_request_cancel_requires_loan_utxo() -> None:
    with pytest.raises(ValueError, match="REQUEST_CANCEL requires params.loan_utxo"):
        FluidTokensTxBuilder().resolve_snapshot(
            backend=None,
            market_name="m",
            action=LendingAction.REQUEST_CANCEL,
            params=ActionParams(actor_address="addr1b"),
        )


def test_resolve_snapshot_request_create_directs_to_from_backend() -> None:
    with pytest.raises(NotImplementedError, match="REQUEST_CREATE"):
        FluidTokensTxBuilder().resolve_snapshot(
            backend=None,
            market_name="m",
            action=LendingAction.REQUEST_CREATE,
            params=ActionParams(actor_address="addr1b"),
        )


def test_resolve_snapshot_modify_collateral_directs_to_from_backend() -> None:
    with pytest.raises(
        NotImplementedError,
        match="ChangeCollateralSnapshot.from_backend",
    ):
        FluidTokensTxBuilder().resolve_snapshot(
            backend=None,
            market_name="m",
            action=LendingAction.MODIFY_COLLATERAL,
            params=ActionParams(actor_address="addr1b"),
        )
