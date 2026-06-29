import pytest

from charli3_dendrite.lending.danogo.transactions import builder as builder_module
from charli3_dendrite.lending.danogo.transactions.builder import DanogoTxBuilder
from charli3_dendrite.lending.danogo.transactions.context import IncreaseLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import (
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.transactions.base import (
    AbstractLendingTxBuilder,
    ActionParams,
    LendingAction,
)

# A well-formed mainnet payment address; only its bech32 decoding is exercised here.
_ACTOR = (
    "addr1qy6fgvsmep5yxpdtyfsyn26dddswns92qqg0ysv8snkc3j5"
    "ej7za8s8pvffkjd9m4jfkwrcanendk8qvrjp80cz08y7qfxa5hd"
)


class _StubBuilder(AbstractLendingTxBuilder):
    @classmethod
    def protocol(cls) -> str:
        return "Stub"

    @classmethod
    def supported_actions(cls) -> set[LendingAction]:
        return {LendingAction.DEPOSIT}

    def resolve_snapshot(self, backend, *, market_name, action, params):  # noqa: ANN001
        raise NotImplementedError

    def contribute(self, action, tx_builder, *, snapshot, params):  # noqa: ANN001
        raise NotImplementedError


def test_supported_actions_and_guard():
    b = _StubBuilder()
    assert LendingAction.DEPOSIT in b.supported_actions()
    # build_and_evaluate must reject an unsupported action before any backend call
    with pytest.raises(ValueError, match="does not support"):
        b.build_and_evaluate(
            backend=None,
            market_name="m",
            action=LendingAction.WITHDRAW,
            params=ActionParams(actor_address="addr", amount=1),
        )


def test_danogo_advertises_repay_and_increase():
    actions = DanogoTxBuilder.supported_actions()
    assert LendingAction.REPAY in actions
    assert LendingAction.INCREASE in actions
    assert LendingAction.MODIFY_COLLATERAL in actions
    assert {
        LendingAction.BORROW,
        LendingAction.DEPOSIT,
        LendingAction.WITHDRAW,
        LendingAction.REPAY,
        LendingAction.INCREASE,
        LendingAction.MODIFY_COLLATERAL,
    } <= actions


def test_repay_requires_loan_utxo():
    b = DanogoTxBuilder()
    # REPAY resolution must fail clearly (before any backend read) when the loan
    # out-ref to spend is missing.
    with pytest.raises(ValueError, match="REPAY requires params.loan_utxo"):
        b.resolve_snapshot(
            None,
            market_name="m",
            action=LendingAction.REPAY,
            params=ActionParams(actor_address="addr", amount=1),
        )


def _capture_repay(monkeypatch):
    """Patch the builder's repay primitives, returning the captured call kwargs."""
    calls: dict[str, dict] = {}

    def fake_build_repay(
        tx_builder, *, snapshot, amount, collateral=None, **_
    ):  # noqa: ANN001
        calls["build_repay"] = {"amount": amount, "collateral": collateral}

    def fake_add_repay_funding(
        tx_builder,  # noqa: ANN001
        *,
        snapshot,  # noqa: ANN001
        actor,  # noqa: ANN001
        actor_utxo,  # noqa: ANN001
        amount,  # noqa: ANN001
        collateral=None,
        **_,
    ):
        calls["add_repay_funding"] = {"amount": amount, "collateral": collateral}
        return {"funding": True}

    monkeypatch.setattr(builder_module, "build_repay", fake_build_repay)
    monkeypatch.setattr(builder_module, "add_repay_funding", fake_add_repay_funding)
    return calls


def test_repay_routes_collateral_target(monkeypatch):
    # A REPAY carrying a collateral target threads it into both repay primitives as
    # the absolute target map (here a same-unit reduction).
    calls = _capture_repay(monkeypatch)
    snapshot = RepaySnapshot.__new__(RepaySnapshot)
    target = {"abc": 6_000_000_000}
    DanogoTxBuilder().contribute(
        LendingAction.REPAY,
        object(),
        snapshot=snapshot,
        params=ActionParams(
            actor_address=_ACTOR,
            amount=1,
            collateral=dict(target),
            actor_utxo="ref#0",
            loan_utxo="loan#0",
        ),
    )
    assert calls["build_repay"]["collateral"] == target
    assert calls["add_repay_funding"]["collateral"] == target


def test_plain_repay_passes_no_collateral(monkeypatch):
    # A REPAY with no collateral target leaves the loan's collateral unchanged: the
    # primitives receive `None`, not an empty mapping.
    calls = _capture_repay(monkeypatch)
    snapshot = RepaySnapshot.__new__(RepaySnapshot)
    DanogoTxBuilder().contribute(
        LendingAction.REPAY,
        object(),
        snapshot=snapshot,
        params=ActionParams(
            actor_address=_ACTOR,
            amount=1,
            actor_utxo="ref#0",
            loan_utxo="loan#0",
        ),
    )
    assert calls["build_repay"]["collateral"] is None
    assert calls["add_repay_funding"]["collateral"] is None


def test_increase_requires_loan_utxo():
    b = DanogoTxBuilder()
    # INCREASE resolution must fail clearly (before any backend read) when the loan
    # out-ref to increase is missing.
    with pytest.raises(ValueError, match="INCREASE requires params.loan_utxo"):
        b.resolve_snapshot(
            None,
            market_name="m",
            action=LendingAction.INCREASE,
            params=ActionParams(actor_address="addr", borrow_amount=1),
        )


def _capture_increase(monkeypatch):
    """Patch the builder's increase primitives, returning the captured call kwargs."""
    calls: dict[str, dict] = {}

    def fake_safe_increase_amount(snapshot, *, borrow_amount, **_):  # noqa: ANN001
        calls["safe_increase_amount"] = {"borrow_amount": borrow_amount}
        return borrow_amount

    def fake_build_increase_loan(
        tx_builder, *, snapshot, borrow_amount, **_
    ):  # noqa: ANN001
        calls["build_increase_loan"] = {"borrow_amount": borrow_amount}

    def fake_add_increase_funding(
        tx_builder, *, snapshot, actor, actor_utxo, **_
    ):  # noqa: ANN001
        calls["add_increase_funding"] = {"actor_utxo": actor_utxo}
        return {"funding": True}

    monkeypatch.setattr(
        builder_module, "safe_increase_amount", fake_safe_increase_amount
    )
    monkeypatch.setattr(builder_module, "build_increase_loan", fake_build_increase_loan)
    monkeypatch.setattr(
        builder_module, "add_increase_funding", fake_add_increase_funding
    )
    return calls


def test_increase_routes_borrow_amount(monkeypatch):
    # An INCREASE threads `borrow_amount` through the preflight, the builder, and the
    # funding, in that order, and funds from the pinned actor UTxO.
    calls = _capture_increase(monkeypatch)
    snapshot = IncreaseLoanSnapshot.__new__(IncreaseLoanSnapshot)
    DanogoTxBuilder().contribute(
        LendingAction.INCREASE,
        object(),
        snapshot=snapshot,
        params=ActionParams(
            actor_address=_ACTOR,
            borrow_amount=500_000_000,
            actor_utxo="ref#0",
            loan_utxo="loan#0",
        ),
    )
    assert calls["safe_increase_amount"]["borrow_amount"] == 500_000_000
    assert calls["build_increase_loan"]["borrow_amount"] == 500_000_000
    assert calls["add_increase_funding"]["actor_utxo"] == "ref#0"


def test_increase_requires_borrow_amount(monkeypatch):
    # An INCREASE missing the borrow amount fails clearly before any build work.
    _capture_increase(monkeypatch)
    snapshot = IncreaseLoanSnapshot.__new__(IncreaseLoanSnapshot)
    with pytest.raises(ValueError, match="INCREASE requires params.borrow_amount"):
        DanogoTxBuilder().contribute(
            LendingAction.INCREASE,
            object(),
            snapshot=snapshot,
            params=ActionParams(
                actor_address=_ACTOR,
                actor_utxo="ref#0",
                loan_utxo="loan#0",
            ),
        )


def test_modify_collateral_requires_loan_utxo():
    b = DanogoTxBuilder()
    # MODIFY_COLLATERAL resolution must fail clearly (before any backend read) when the
    # loan out-ref to modify is missing.
    with pytest.raises(ValueError, match="MODIFY_COLLATERAL requires params.loan_utxo"):
        b.resolve_snapshot(
            None,
            market_name="m",
            action=LendingAction.MODIFY_COLLATERAL,
            params=ActionParams(actor_address="addr", collateral={"abc": 1}),
        )


def _capture_modify(monkeypatch):
    """Patch the builder's modify primitives, returning the captured call kwargs."""
    calls: dict[str, dict] = {}

    def fake_safe_collateral_for_modify(
        snapshot, *, target_collateral, **_
    ):  # noqa: ANN001
        calls["safe_collateral_for_modify"] = {"target_collateral": target_collateral}
        return 1

    def fake_build_modify_collateral(
        tx_builder, *, snapshot, target_collateral, **_
    ):  # noqa: ANN001
        calls["build_modify_collateral"] = {"target_collateral": target_collateral}

    def fake_add_modify_collateral_funding(
        tx_builder,  # noqa: ANN001
        *,
        snapshot,  # noqa: ANN001
        actor,  # noqa: ANN001
        actor_utxo,  # noqa: ANN001
        target_collateral=None,
        **_,
    ):
        calls["add_modify_collateral_funding"] = {
            "actor_utxo": actor_utxo,
            "target_collateral": target_collateral,
        }
        return {"funding": True}

    monkeypatch.setattr(
        builder_module, "safe_collateral_for_modify", fake_safe_collateral_for_modify
    )
    monkeypatch.setattr(
        builder_module, "build_modify_collateral", fake_build_modify_collateral
    )
    monkeypatch.setattr(
        builder_module,
        "add_modify_collateral_funding",
        fake_add_modify_collateral_funding,
    )
    return calls


def test_modify_collateral_routes_target_collateral(monkeypatch):
    # A MODIFY_COLLATERAL threads the TARGET collateral through the preflight, the
    # builder, and the funding, and funds from the pinned actor UTxO.
    calls = _capture_modify(monkeypatch)
    snapshot = ModifyCollateralSnapshot.__new__(ModifyCollateralSnapshot)
    target = {"abc": 7_000_000_000}
    DanogoTxBuilder().contribute(
        LendingAction.MODIFY_COLLATERAL,
        object(),
        snapshot=snapshot,
        params=ActionParams(
            actor_address=_ACTOR,
            collateral=dict(target),
            actor_utxo="ref#0",
            loan_utxo="loan#0",
        ),
    )
    assert calls["safe_collateral_for_modify"]["target_collateral"] == target
    assert calls["build_modify_collateral"]["target_collateral"] == target
    assert calls["add_modify_collateral_funding"]["target_collateral"] == target
    assert calls["add_modify_collateral_funding"]["actor_utxo"] == "ref#0"


def test_modify_collateral_requires_collateral(monkeypatch):
    # A MODIFY_COLLATERAL with no target collateral fails clearly before any build work.
    _capture_modify(monkeypatch)
    snapshot = ModifyCollateralSnapshot.__new__(ModifyCollateralSnapshot)
    with pytest.raises(
        ValueError, match="MODIFY_COLLATERAL requires params.collateral"
    ):
        DanogoTxBuilder().contribute(
            LendingAction.MODIFY_COLLATERAL,
            object(),
            snapshot=snapshot,
            params=ActionParams(
                actor_address=_ACTOR,
                actor_utxo="ref#0",
                loan_utxo="loan#0",
            ),
        )
