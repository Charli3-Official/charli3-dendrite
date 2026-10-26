"""The Liqwid V2 builder behind the protocol-agnostic lending seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pycardano import Transaction
from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions import builder as builder_module
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.builder import LiqwidTxBuilder
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.combos import (
    RemoveAndRedeemSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    SupplyAndBorrowSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.errors import ActionUtxoSpentError
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.registry import available_lending_protocols
from charli3_dendrite.lending.registry import get_lending_builder
from charli3_dendrite.lending.transactions.base import ActionParams
from charli3_dendrite.lending.transactions.base import LendingAction
from charli3_dendrite.lending.transactions.errors import OgmiosEvaluationError
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.backend import capture_backend
from tests.lending.liqwid.transactions.replay import block_slot
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import captured_redeemers
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import redeemers

_WALLET = "addr_test"
_REF = "aa" * 32
QADA = "a04ce7a52545e5e33c2867e148898d9e667a69602285f6a1298f9d68"


def test_registered_as_liqwid() -> None:
    assert get_lending_builder("liqwid") is LiqwidTxBuilder
    assert "liqwid" in available_lending_protocols()
    assert LiqwidTxBuilder.protocol() == "LiqwidV2"
    assert LiqwidTxBuilder.supported_actions() == {
        LendingAction.DEPOSIT,
        LendingAction.WITHDRAW,
        LendingAction.BORROW,
        LendingAction.INCREASE,
        LendingAction.REPAY,
        LendingAction.MODIFY_COLLATERAL,
    }


# --- contribute -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "snapshot_cls", "label"),
    [
        (LendingAction.DEPOSIT, SupplySnapshot, "supply_ada"),
        (LendingAction.WITHDRAW, RedeemSnapshot, "redeem_ada"),
        (LendingAction.WITHDRAW, RemoveAndRedeemSnapshot, "remove_and_redeem_a"),
        (LendingAction.BORROW, BorrowSnapshot, "open_single"),
        (LendingAction.BORROW, SupplyAndBorrowSnapshot, "supply_and_borrow_ada_night"),
        (LendingAction.INCREASE, IncreaseSnapshot, "increase_ada"),
        (LendingAction.REPAY, RepaySnapshot, "repay_usdm"),
        (LendingAction.MODIFY_COLLATERAL, CollateralSnapshot, "collateral_add"),
    ],
)
def test_contribute_builds_the_captured_action(
    action: LendingAction,
    snapshot_cls: Any,  # noqa: ANN401
    label: str,
) -> None:
    fix = fixture(label)

    def contribute(tx_builder: TransactionBuilder, *, snapshot: object) -> None:
        LiqwidTxBuilder().contribute(
            action,
            tx_builder,
            snapshot=snapshot,  # type: ignore[arg-type]
            params=ActionParams(actor_address=_WALLET),
        )

    built = build(contribute, snapshot_cls.from_capture(fix), slot=block_slot(fix))
    assert redeemers(built) == captured_redeemers(fix)


def _empty_builder() -> TransactionBuilder:
    return TransactionBuilder(EvalContext(last_block_slot=0))


def test_contribute_checks_the_snapshot_type() -> None:
    snapshot = RepaySnapshot.from_capture(fixture("repay_usdm"))
    with pytest.raises(
        TypeError,
        match="borrow expects BorrowSnapshot or SupplyAndBorrowSnapshot, got "
        "RepaySnapshot",
    ):
        LiqwidTxBuilder().contribute(
            LendingAction.BORROW,
            _empty_builder(),
            snapshot=snapshot,
            params=ActionParams(actor_address=_WALLET),
        )


@pytest.mark.parametrize("action", [LendingAction.RECAST, LendingAction.LEND])
def test_contribute_refuses_an_unsupported_action(action: LendingAction) -> None:
    snapshot = RepaySnapshot.from_capture(fixture("repay_usdm"))
    with pytest.raises(ValueError, match="does not support"):
        LiqwidTxBuilder().contribute(
            action,
            _empty_builder(),
            snapshot=snapshot,
            params=ActionParams(actor_address=_WALLET),
        )


# --- resolve_snapshot -------------------------------------------------------------


def _record(monkeypatch: pytest.MonkeyPatch, cls: type) -> dict[str, Any]:
    """Replace ``cls.from_backend`` with a recorder of its arguments.

    The recorder returns a stand-in snapshot on the Ada market.
    """
    calls: dict[str, Any] = {}

    def from_backend(_backend: object, **kwargs: Any) -> object:  # noqa: ANN401
        calls.update(kwargs)
        return SimpleNamespace(market=SimpleNamespace(name="Ada"))

    monkeypatch.setattr(cls, "from_backend", staticmethod(from_backend))
    return calls


def _resolve(
    action: LendingAction,
    *,
    market_name: str = "",
    backend: object | None = None,
    **params: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    return LiqwidTxBuilder().resolve_snapshot(
        object() if backend is None else backend,  # type: ignore[arg-type]
        market_name=market_name,
        action=action,
        params=ActionParams(actor_address=_WALLET, **params),
    )


def test_resolve_deposit_supplies_the_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch, SupplySnapshot)
    _resolve(LendingAction.DEPOSIT, market_name="Ada", amount=5)
    assert calls == {
        "market": "Ada",
        "owner_address": _WALLET,
        "amount": 5,
        "funding": None,
    }


def test_resolve_withdraw_redeems_the_underlying_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, RedeemSnapshot)
    _resolve(LendingAction.WITHDRAW, market_name="DJED", amount=5)
    assert calls == {
        "market": "DJED",
        "owner_address": _WALLET,
        "amount": 5,
        "funding": None,
    }


def test_resolve_withdraw_of_nothing_points_at_withdraw_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, RedeemSnapshot)
    with pytest.raises(ValueError, match="withdraw_all=True"):
        _resolve(LendingAction.WITHDRAW, market_name="Ada")
    assert calls == {}


def test_resolve_borrow_opens_against_the_collateral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, BorrowSnapshot)
    _resolve(
        LendingAction.BORROW,
        market_name="Ada",
        borrow_amount=7,
        collateral={QADA: 9},
    )
    assert calls == {
        "market": "Ada",
        "owner_address": _WALLET,
        "amount": 7,
        "collateral": {QADA: 9},
        "funding": None,
    }


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"collateral": {QADA: 9}}, "borrow_amount"),
        ({"borrow_amount": 7}, "collateral"),
    ],
)
def test_resolve_borrow_needs_an_amount_and_collateral(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    match: str,
) -> None:
    calls = _record(monkeypatch, BorrowSnapshot)
    with pytest.raises(ValueError, match=match):
        _resolve(LendingAction.BORROW, market_name="Ada", **params)
    assert calls == {}


def test_resolve_increase_keeps_the_collateral_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, IncreaseSnapshot)
    _resolve(
        LendingAction.INCREASE,
        loan_utxo=f"{_REF}#1",
        borrow_amount=7,
        collateral={},
    )
    assert calls == {
        "loan_utxo": f"{_REF}#1",
        "owner_address": _WALLET,
        "amount": 7,
        "collateral": None,
        "funding": None,
    }


def test_resolve_increase_passes_a_collateral_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, IncreaseSnapshot)
    _resolve(
        LendingAction.INCREASE,
        loan_utxo=f"{_REF}#1",
        borrow_amount=7,
        collateral={QADA: 9},
    )
    assert calls["collateral"] == {QADA: 9}


def test_resolve_repay_pays_the_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch, RepaySnapshot)
    _resolve(LendingAction.REPAY, loan_utxo=f"{_REF}#2", amount=11)
    assert calls == {
        "loan_utxo": f"{_REF}#2",
        "owner_address": _WALLET,
        "repay": 11,
        "collateral": None,
        "funding": None,
    }
    _resolve(
        LendingAction.REPAY, loan_utxo=f"{_REF}#2", amount=11, collateral={QADA: 3}
    )
    assert calls["collateral"] == {QADA: 3}


def test_resolve_modify_collateral_takes_an_empty_target_as_remove_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, CollateralSnapshot)
    _resolve(LendingAction.MODIFY_COLLATERAL, loan_utxo=f"{_REF}#3")
    assert calls == {
        "loan_utxo": f"{_REF}#3",
        "owner_address": _WALLET,
        "collateral": {},
        "funding": None,
    }


@pytest.mark.parametrize(
    ("action", "cls", "params"),
    [
        (LendingAction.INCREASE, IncreaseSnapshot, {"borrow_amount": 7}),
        (LendingAction.REPAY, RepaySnapshot, {"amount": 7}),
        (LendingAction.MODIFY_COLLATERAL, CollateralSnapshot, {}),
    ],
)
def test_a_loan_action_needs_the_loan(
    monkeypatch: pytest.MonkeyPatch,
    action: LendingAction,
    cls: type,
    params: dict[str, Any],
) -> None:
    calls = _record(monkeypatch, cls)
    with pytest.raises(ValueError, match="must name the loan"):
        _resolve(action, **params)
    assert calls == {}


def test_resolve_increase_needs_an_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch, IncreaseSnapshot)
    with pytest.raises(ValueError, match="borrow_amount"):
        _resolve(LendingAction.INCREASE, loan_utxo=f"{_REF}#1")
    assert calls == {}


def test_a_loan_action_on_another_market_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, RepaySnapshot)
    _resolve(LendingAction.REPAY, market_name="Ada", loan_utxo=f"{_REF}#2", amount=1)
    with pytest.raises(ValueError, match="is on market Ada, not DJED"):
        _resolve(
            LendingAction.REPAY,
            market_name="DJED",
            loan_utxo=f"{_REF}#2",
            amount=1,
        )


def test_resolve_refuses_an_unsupported_action() -> None:
    with pytest.raises(ValueError, match="does not support"):
        _resolve(LendingAction.RECAST, loan_utxo=f"{_REF}#0")


def test_a_pinned_actor_utxo_is_the_funding(monkeypatch: pytest.MonkeyPatch) -> None:
    fix = fixture("supply_ada")
    wallet = utxo_from_dict(next(u for u in fix["inputs"] if not u["datum"]))
    tx_hash, index = wallet.out_ref
    calls = _record(monkeypatch, SupplySnapshot)
    _resolve(
        LendingAction.DEPOSIT,
        market_name="Ada",
        backend=FixtureBackend(utxos=[wallet]),
        amount=5,
        actor_utxo=f"{tx_hash}#{index}",
    )
    assert calls["funding"] == [wallet]


# --- build_and_evaluate: a spent action UTxO ------------------------------------

# A supply on Ada over two live Ada action UTxOs: the captured supply's and the
# captured redeem's.
_SUPPLY = "supply_ada"
_OTHER = "redeem_ada"


def _ada_action(label: str) -> ActionUtxo:
    ada = default_deployment().market_by_name("Ada")
    (action,) = CapturedTx.of(fixture(label)).action_inputs(ada)
    return action


def _two_action_backend() -> FixtureBackend:
    backend = capture_backend(fixture(_SUPPLY))
    backend.records.append(utxo_to_info(_ada_action(_OTHER).utxo))
    return backend


def _supply_params() -> ActionParams:
    fix = fixture(_SUPPLY)
    wallet = next(u["address"] for u in fix["inputs"] if not u["datum"])
    return ActionParams(actor_address=wallet, amount=100_000_000)


def _missing_error(missing: list[str], *, nested: bool) -> OgmiosEvaluationError:
    """The Ogmios error for a transaction whose inputs ``missing`` are unknown.

    ``nested`` gives the shape Ogmios returns for a transaction with redeemers: a
    script failure (3010) whose per-validator errors carry a 3004 naming each
    input. Otherwise the 3004 is the top-level error.
    """
    if not nested:
        return OgmiosEvaluationError(
            {
                "code": 3004,
                "message": (
                    "Unable to create the evaluation context from the given "
                    "transaction. Some inputs are missing from UTxO set: "
                    + ", ".join(missing)
                ),
                "data": {},
            },
        )
    return OgmiosEvaluationError(
        {
            "code": 3010,
            "message": "Some scripts of the transactions terminated with error(s).",
            "data": [
                {
                    "validator": {"index": 0, "purpose": "mint"},
                    "error": {
                        "code": 3004,
                        "message": (
                            "Unable to create the evaluation context from the "
                            "given transaction."
                        ),
                        "data": {
                            "reason": (
                                "Unknown transaction input (missing from UTxO "
                                f"set): {ref}"
                            ),
                        },
                    },
                }
                for ref in missing
            ],
        },
    )


def _spent_inputs(
    monkeypatch: pytest.MonkeyPatch,
    spent: set[str],
    *,
    nested: bool = True,
) -> list[list[dict[str, Any]]]:
    """Make the evaluation report the inputs in ``spent`` as missing."""
    sent: list[list[dict[str, Any]]] = []

    def evaluate(tx_cbor: str, additional_utxo: list[dict[str, Any]]) -> list:
        sent.append(additional_utxo)
        inputs = {
            f"{bytes(i.transaction_id).hex()}#{i.index}"
            for i in Transaction.from_cbor(tx_cbor).transaction_body.inputs
        }
        missing = sorted(inputs & spent)
        if not missing:
            return [{"validator": {"index": 0, "purpose": "spend"}}]
        raise _missing_error(missing, nested=nested)

    monkeypatch.setattr(builder_module, "evaluate_tx_cbor", evaluate)
    return sent


@pytest.mark.parametrize("nested", [True, False], ids=["script-failure", "3004"])
def test_a_spent_action_utxo_is_named_and_a_retry_excluding_it_picks_another(
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    actions = {_ada_action(_SUPPLY).out_ref, _ada_action(_OTHER).out_ref}
    _spent_inputs(monkeypatch, actions, nested=nested)
    backend = _two_action_backend()
    builder = LiqwidTxBuilder()
    with pytest.raises(ActionUtxoSpentError) as raised:
        builder.build_and_evaluate(
            backend=backend,
            market_name="Ada",
            action=LendingAction.DEPOSIT,
            params=_supply_params(),
        )
    chosen = builder.last_snapshot
    assert isinstance(chosen, SupplySnapshot)
    assert raised.value.out_ref == chosen.action.out_ref
    assert isinstance(raised.value.__cause__, OgmiosEvaluationError)

    retry = SupplySnapshot.from_backend(
        backend,
        market="Ada",
        owner_address=_supply_params().actor_address,
        amount=100_000_000,
        exclude={raised.value.out_ref},
    )
    assert retry.action.out_ref == (actions - {raised.value.out_ref}).pop()


def test_build_and_evaluate_returns_the_budgets_and_keeps_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _spent_inputs(monkeypatch, set())
    builder = LiqwidTxBuilder()
    budgets = builder.build_and_evaluate(
        backend=_two_action_backend(),
        market_name="Ada",
        action=LendingAction.DEPOSIT,
        params=_supply_params(),
    )
    assert budgets == [{"validator": {"index": 0, "purpose": "spend"}}]
    snapshot = builder.last_snapshot
    assert isinstance(snapshot, SupplySnapshot)
    # Only the wallet funding is sent as additional UTxOs.
    funding = {f"{h}#{i}" for h, i in (u.out_ref for u in snapshot.funding)}
    assert {f"{e['transaction']['id']}#{e['index']}" for e in sent[0]} == funding


def test_a_missing_input_other_than_the_action_utxo_is_left_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture(_SUPPLY)
    funding = {
        f"{u['out_ref'][0]}#{u['out_ref'][1]}" for u in fix["inputs"] if not u["datum"]
    }
    _spent_inputs(monkeypatch, funding)
    with pytest.raises(OgmiosEvaluationError) as raised:
        LiqwidTxBuilder().build_and_evaluate(
            backend=_two_action_backend(),
            market_name="Ada",
            action=LendingAction.DEPOSIT,
            params=_supply_params(),
        )
    assert not isinstance(raised.value, ActionUtxoSpentError)
    assert raised.value.code == 3010
    assert {f"{h}#{i}" for h, i in raised.value.missing_inputs()} == funding


def test_build_and_evaluate_refuses_an_unsupported_action() -> None:
    with pytest.raises(ValueError, match="does not support"):
        LiqwidTxBuilder().build_and_evaluate(
            backend=object(),  # type: ignore[arg-type]
            market_name="Ada",
            action=LendingAction.RECAST,
            params=ActionParams(actor_address=_WALLET),
        )


@pytest.mark.parametrize(
    ("snapshot_cls", "label", "legs"),
    [
        (SupplyAndBorrowSnapshot, "supply_and_borrow_ada_night", ("supply", "borrow")),
        (RemoveAndRedeemSnapshot, "remove_and_redeem_a", ("redeem",)),
        (RepaySnapshot, "close_zero_debt", ()),
        (CollateralSnapshot, "collateral_add", ()),
        (BorrowSnapshot, "open_single", (None,)),
    ],
)
def test_the_chosen_action_utxos_of_each_snapshot(
    snapshot_cls: Any,  # noqa: ANN401
    label: str,
    legs: tuple[str | None, ...],
) -> None:
    snapshot = snapshot_cls.from_capture(fixture(label))
    expected = {
        (snapshot if leg is None else getattr(snapshot, leg)).action.out_ref
        for leg in legs
    }
    assert builder_module.chosen_action_utxos(snapshot) == expected
