"""The V4 builder behind the protocol-agnostic lending seam."""

from __future__ import annotations

from typing import Any

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.state import collateral_asset_unit
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import PoolBorrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.builder import (
    FluidTokensV4TxBuilder,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import RecastSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.registry import get_lending_builder
from charli3_dendrite.lending.transactions.base import ActionParams
from charli3_dendrite.lending.transactions.base import LendingAction
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture

_WALLET = "addr_test"


def test_registered_as_fluidtokens_v4() -> None:
    assert get_lending_builder("fluidtokens_v4") is FluidTokensV4TxBuilder
    assert FluidTokensV4TxBuilder.protocol() == "FluidTokensV4"
    assert FluidTokensV4TxBuilder.supported_actions() == {
        LendingAction.BORROW,
        LendingAction.REPAY,
        LendingAction.MODIFY_COLLATERAL,
        LendingAction.RECAST,
    }


@pytest.mark.parametrize(
    ("action", "snapshot_cls", "name"),
    [
        (LendingAction.BORROW, BorrowSnapshot, "borrow_single"),
        (LendingAction.REPAY, RepaySnapshot, "repay_single"),
        (
            LendingAction.MODIFY_COLLATERAL,
            ChangeCollateralSnapshot,
            "change_collateral_single",
        ),
    ],
)
def test_contribute_builds_the_action(
    action: LendingAction,
    snapshot_cls: type,
    name: str,
) -> None:
    from pycardano import Transaction

    from charli3_dendrite.lending.transactions.infra import assemble_unsigned
    from tests.lending.fluidtokens_v4.transactions.replay import redeemers

    fix = fixture(name)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    FluidTokensV4TxBuilder().contribute(
        action,
        tx_builder,
        snapshot=snapshot_cls.from_capture(fix),
        params=ActionParams(actor_address=_WALLET),
    )
    tx = Transaction.from_cbor(assemble_unsigned(tx_builder))
    assert redeemers(tx) == captured_redeemers(fix)


def test_contribute_checks_the_snapshot_type() -> None:
    fix = fixture("repay_single")
    with pytest.raises(TypeError, match="expects BorrowSnapshot"):
        FluidTokensV4TxBuilder().contribute(
            LendingAction.BORROW,
            TransactionBuilder(EvalContext(last_block_slot=0)),
            snapshot=RepaySnapshot.from_capture(fix),
            params=ActionParams(actor_address=_WALLET),
        )
    with pytest.raises(ValueError, match="does not support"):
        FluidTokensV4TxBuilder().contribute(
            LendingAction.DEPOSIT,
            TransactionBuilder(EvalContext(last_block_slot=0)),
            snapshot=RepaySnapshot.from_capture(fix),
            params=ActionParams(actor_address=_WALLET),
        )


def _record(monkeypatch: pytest.MonkeyPatch, cls: type) -> dict[str, Any]:
    """Replace ``cls.from_backend`` with a recorder of its arguments."""
    calls: dict[str, Any] = {}

    def from_backend(_backend: object, **kwargs: Any) -> object:  # noqa: ANN401
        calls.update(kwargs)
        return calls

    monkeypatch.setattr(cls, "from_backend", staticmethod(from_backend))
    return calls


def _resolve(action: LendingAction, **params: Any) -> Any:  # noqa: ANN401
    return FluidTokensV4TxBuilder().resolve_snapshot(
        object(),  # type: ignore[arg-type]
        market_name="",
        action=action,
        params=ActionParams(actor_address=_WALLET, **params),
    )


_REF = "aa" * 32


def test_resolve_repay_repays_the_loan_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch, RepaySnapshot)
    _resolve(LendingAction.REPAY, loan_utxo=f"{_REF}#1")
    assert calls == {"loans": [(_REF, 1)], "borrower_address": _WALLET}


def test_resolve_repay_refuses_a_chosen_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, RepaySnapshot)
    with pytest.raises(ValueError, match="params.amount must be 0"):
        _resolve(LendingAction.REPAY, loan_utxo=f"{_REF}#1", amount=5)
    assert calls == {}


def test_resolve_recast_pays_the_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch, RecastSnapshot)
    _resolve(LendingAction.RECAST, loan_utxo=f"{_REF}#0", amount=7)
    assert calls == {"recasts": [((_REF, 0), 7)], "borrower_address": _WALLET}


def test_resolve_needs_the_loan(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch, RepaySnapshot)
    with pytest.raises(ValueError, match="must name the loan"):
        _resolve(LendingAction.REPAY)


def test_resolve_modify_collateral_checks_the_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = ChangeCollateralSnapshot.from_capture(fixture("change_collateral_single"))
    monkeypatch.setattr(
        ChangeCollateralSnapshot,
        "from_backend",
        staticmethod(lambda _backend, **_: capture),
    )
    unit = capture.positions[0].collateral_unit
    assert (
        _resolve(
            LendingAction.MODIFY_COLLATERAL,
            loan_utxo=f"{_REF}#0",
            collateral={unit: 5},
        )
        is capture
    )
    with pytest.raises(ValueError, match="not collateralised in lovelace"):
        _resolve(
            LendingAction.MODIFY_COLLATERAL,
            loan_utxo=f"{_REF}#0",
            collateral={"lovelace": 5},
        )


def test_resolve_borrow_picks_the_collateral_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    pool = next(
        utxo_from_dict(u)
        for u in fix["inputs"]
        if any(p == c.POOL_POLICY for p, _, _ in u["assets"])
    )
    calls = _record(monkeypatch, BorrowSnapshot)
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens_v4.transactions.resolve." "resolve_utxo",
        lambda _backend, *_: pool,
    )
    (option, *_) = PoolDatum.from_cbor(pool.datum).collateral_options
    strike = collateral_asset_unit(option)
    _resolve(
        LendingAction.BORROW,
        loan_utxo=f"{pool.out_ref[0]}#{pool.out_ref[1]}",
        borrow_amount=5_000_000,
        collateral={strike: 0},
    )
    assert calls["borrows"] == [PoolBorrow(pool.out_ref, 5_000_000, 0, None)]
    with pytest.raises(ValueError, match="does not take lovelace"):
        _resolve(
            LendingAction.BORROW,
            loan_utxo=f"{pool.out_ref[0]}#{pool.out_ref[1]}",
            borrow_amount=5_000_000,
            collateral={"lovelace": 1},
        )
    with pytest.raises(ValueError, match="borrow_amount"):
        _resolve(LendingAction.BORROW, loan_utxo=f"{pool.out_ref[0]}#0")
