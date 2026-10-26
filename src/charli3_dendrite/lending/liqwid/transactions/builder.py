"""Liqwid V2 implementation of the lending transaction seam.

``resolve_snapshot`` maps :class:`ActionParams` onto one flow's ``from_backend``;
``contribute`` adds a resolved snapshot by its type. The two combinations have no
``LendingAction`` of their own: resolve them with
``SupplyAndBorrowSnapshot.from_backend`` / ``RemoveAndRedeemSnapshot.from_backend``
and contribute them under ``BORROW`` / ``WITHDRAW``. A redeem of a wallet's whole
qToken balance is ``RedeemSnapshot.from_backend(..., withdraw_all=True)``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import build_increase
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import build_collateral
from charli3_dendrite.lending.liqwid.transactions.combos import RemoveAndRedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.combos import SupplyAndBorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.combos import build_remove_and_redeem
from charli3_dendrite.lending.liqwid.transactions.combos import build_supply_and_borrow
from charli3_dendrite.lending.liqwid.transactions.errors import ActionUtxoSpentError
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.repay import build_repay
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import build_supply
from charli3_dendrite.lending.transactions.base import AbstractLendingTxBuilder
from charli3_dendrite.lending.transactions.base import LendingAction
from charli3_dendrite.lending.transactions.errors import OgmiosEvaluationError
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from charli3_dendrite.lending.transactions.infra import current_slot
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor
from charli3_dendrite.lending.transactions.infra import parse_out_ref
from charli3_dendrite.lending.transactions.resolve import resolve_utxo_by_outref
from charli3_dendrite.lending.units import out_ref_text

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.transactions.base import ActionParams
    from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = ["LiqwidTxBuilder", "chosen_action_utxos"]

# Each action's snapshot types and the function that adds each to a builder. A
# combination is contributed under the action of the leg that names it.
_ACTIONS: dict[
    LendingAction,
    tuple[tuple[type[PoolActionSnapshot], Callable[..., None]], ...],
] = {
    LendingAction.DEPOSIT: ((SupplySnapshot, build_supply),),
    LendingAction.WITHDRAW: (
        (RedeemSnapshot, build_redeem),
        (RemoveAndRedeemSnapshot, build_remove_and_redeem),
    ),
    LendingAction.BORROW: (
        (BorrowSnapshot, build_borrow),
        (SupplyAndBorrowSnapshot, build_supply_and_borrow),
    ),
    LendingAction.INCREASE: ((IncreaseSnapshot, build_increase),),
    LendingAction.REPAY: ((RepaySnapshot, build_repay),),
    LendingAction.MODIFY_COLLATERAL: ((CollateralSnapshot, build_collateral),),
}

# The actions on an existing loan: the loan names the market.
_LOAN_ACTIONS = {
    LendingAction.INCREASE,
    LendingAction.REPAY,
    LendingAction.MODIFY_COLLATERAL,
}


def chosen_action_utxos(snapshot: PoolActionSnapshot) -> frozenset[str]:
    """The ``<tx hash>#<index>`` of every action UTxO ``snapshot`` spends.

    A combination spends its legs' action UTxOs; a zero-debt close and a
    collateral change spend none.
    """
    legs: tuple[Any, ...]
    if isinstance(snapshot, SupplyAndBorrowSnapshot):
        legs = (snapshot.supply, snapshot.borrow)
    elif isinstance(snapshot, RemoveAndRedeemSnapshot):
        legs = (snapshot.redeem,)
    else:
        legs = (snapshot,)
    return frozenset(
        leg.action.out_ref for leg in legs if getattr(leg, "action", None) is not None
    )


def _loan_utxo(params: ActionParams) -> str:
    """``params.loan_utxo``; raises when it is absent."""
    if params.loan_utxo is None:
        msg = "params.loan_utxo must name the loan (tx_hash#index)"
        raise ValueError(msg)
    return params.loan_utxo


def _borrow_amount(params: ActionParams, action: LendingAction) -> int:
    """``params.borrow_amount``; raises when it is absent."""
    if params.borrow_amount is None:
        msg = f"{action.value} requires params.borrow_amount (the amount borrowed)"
        raise ValueError(msg)
    return params.borrow_amount


def _funding(backend: AbstractBackend, params: ActionParams) -> list[Utxo] | None:
    """The pinned funding input ``params.actor_utxo``; None funds from the wallet."""
    if params.actor_utxo is None:
        return None
    ref = parse_out_ref(params.actor_utxo)
    return [resolve_utxo_by_outref(backend, bytes(ref.transaction_id).hex(), ref.index)]


class LiqwidTxBuilder(AbstractLendingTxBuilder):
    """Build Liqwid V2 supply, redeem, borrow, repay and collateral transactions.

    Attributes:
        last_snapshot: The snapshot of the latest :meth:`build_and_evaluate`, so a
            caller can read what it resolved (its action UTxO, its funding).
    """

    last_snapshot: PoolActionSnapshot | None = None

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "LiqwidV2"

    @classmethod
    def supported_actions(cls) -> set[LendingAction]:
        """Supply, redeem, open, borrow more, repay and collateral changes."""
        return set(_ACTIONS)

    def resolve_snapshot(
        self,
        backend: AbstractBackend,
        *,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """Resolve one flow from ``params`` against live state.

        ``market_name`` is the Liqwid market name (``"Ada"``, ``"DJED"``, ...).
        ``actor_address`` is the wallet; its UTxOs fund the transaction, or only
        ``actor_utxo`` when it pins one.

        - DEPOSIT: supply ``amount`` of the market's underlying.
        - WITHDRAW: redeem the fewest qTokens paying at least ``amount`` of the
          underlying; ``amount`` must be positive (a redeem of the whole qToken
          balance is ``RedeemSnapshot.from_backend(..., withdraw_all=True)``).
        - BORROW: open a loan of ``borrow_amount`` holding ``collateral``
          (qToken unit -> quantity).
        - INCREASE: borrow ``borrow_amount`` more on the loan ``loan_utxo``;
          ``collateral`` is the output loan's target, empty keeps the loan's.
        - REPAY: pay ``amount`` on the loan ``loan_utxo``, closing it at the payoff
          or above; ``collateral`` is a partial repay's target, empty keeps the
          loan's.
        - MODIFY_COLLATERAL: set the collateral of the loan ``loan_utxo`` to
          ``collateral``; empty removes it all.

        A loan action reads its market from the loan; a non-empty ``market_name``
        must name that market. That check runs on the resolved snapshot, after the
        flow's own resolution has read the chain.

        Raises:
            ValueError: The action is unsupported, a field it needs is absent, the
                loan is on another market than ``market_name``, or the flow's
                ``from_backend`` refuses its arguments.
            KeyError: No Liqwid market is named ``market_name`` (DEPOSIT,
                WITHDRAW, BORROW).
            LiqwidBuildError: The flow's ``from_backend`` refuses it (a subclass:
                ``MinValueError``, ``CapExceededError``,
                ``InsufficientLiquidityError``, ``CollateralCountError``,
                ``UnhealthyLoanError``, ``RepayAmountError``,
                ``MarketAwaitingBatchError``, ``ActionUtxoSpentError``,
                ``UnsupportedMarketError``, ``UnsupportedLoanError``,
                ``UnsupportedCollateralError``, ``MissingFeedError``).
        """
        if action not in _ACTIONS:
            raise ValueError(
                f"{self.protocol()} does not support action {action.value!r}",
            )
        if action in _LOAN_ACTIONS:
            snapshot = self._resolve_loan_action(backend, action, params)
            market = snapshot.market.name
            if market_name and market != market_name:
                msg = (
                    f"loan {params.loan_utxo} is on market {market}, not {market_name}"
                )
                raise ValueError(msg)
            return snapshot
        return self._resolve_market_action(backend, market_name, action, params)

    def _resolve_market_action(
        self,
        backend: AbstractBackend,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """DEPOSIT, WITHDRAW or BORROW on the market ``market_name``."""
        if action == LendingAction.DEPOSIT:
            return SupplySnapshot.from_backend(
                backend,
                market=market_name,
                owner_address=params.actor_address,
                amount=params.amount,
                funding=_funding(backend, params),
            )
        if action == LendingAction.WITHDRAW:
            if not params.amount:
                msg = (
                    "WITHDRAW redeems params.amount of the underlying and it is 0; "
                    "redeem the whole qToken balance with "
                    "RedeemSnapshot.from_backend(..., withdraw_all=True)"
                )
                raise ValueError(msg)
            return RedeemSnapshot.from_backend(
                backend,
                market=market_name,
                owner_address=params.actor_address,
                amount=params.amount,
                funding=_funding(backend, params),
            )
        amount = _borrow_amount(params, action)
        if not params.collateral:
            msg = "borrow requires params.collateral (qToken unit -> quantity)"
            raise ValueError(msg)
        return BorrowSnapshot.from_backend(
            backend,
            market=market_name,
            owner_address=params.actor_address,
            amount=amount,
            collateral=dict(params.collateral),
            funding=_funding(backend, params),
        )

    def _resolve_loan_action(
        self,
        backend: AbstractBackend,
        action: LendingAction,
        params: ActionParams,
    ) -> IncreaseSnapshot | RepaySnapshot | CollateralSnapshot:
        """INCREASE, REPAY or MODIFY_COLLATERAL of the loan ``params.loan_utxo``."""
        loan_utxo = _loan_utxo(params)
        target = dict(params.collateral) if params.collateral else None
        if action == LendingAction.INCREASE:
            amount = _borrow_amount(params, action)
            return IncreaseSnapshot.from_backend(
                backend,
                loan_utxo=loan_utxo,
                owner_address=params.actor_address,
                amount=amount,
                collateral=target,
                funding=_funding(backend, params),
            )
        if action == LendingAction.REPAY:
            return RepaySnapshot.from_backend(
                backend,
                loan_utxo=loan_utxo,
                owner_address=params.actor_address,
                repay=params.amount,
                collateral=target,
                funding=_funding(backend, params),
            )
        return CollateralSnapshot.from_backend(
            backend,
            loan_utxo=loan_utxo,
            owner_address=params.actor_address,
            collateral=dict(params.collateral),
            funding=_funding(backend, params),
        )

    def contribute(
        self,
        action: LendingAction,
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,  # unread: the snapshot carries every amount
    ) -> None:
        """Add the flow ``snapshot`` describes to ``tx_builder``.

        Raises:
            ValueError: The action is unsupported.
            TypeError: The snapshot is not one the action takes.
            LiqwidBuildError, ValidityWindowError: As the flow's ``build_*``.
        """
        if action not in _ACTIONS:
            raise ValueError(
                f"{self.protocol()} does not support action {action.value!r}",
            )
        for expected, build in _ACTIONS[action]:
            if isinstance(snapshot, expected):
                build(tx_builder, snapshot=snapshot)
                return
        names = " or ".join(expected.__name__ for expected, _ in _ACTIONS[action])
        raise TypeError(
            f"{action.value} expects {names}, got {type(snapshot).__name__}",
        )

    def build_and_evaluate(
        self,
        *,
        backend: AbstractBackend,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> list[dict[str, Any]]:
        """Resolve, contribute, assemble unsigned and evaluate with Ogmios.

        The resolved snapshot is kept as :attr:`last_snapshot`. Only the wallet
        funding is sent as additional UTxOs, so an action UTxO spent since it was
        chosen is reported missing by Ogmios.

        Raises:
            ValueError, KeyError, LiqwidBuildError: As :meth:`resolve_snapshot`
                and the flow's build.
            ActionUtxoSpentError: Ogmios reports a chosen action UTxO missing from
                its UTxO set; resolve again with it in ``exclude``.
            OgmiosEvaluationError: Any other evaluation failure.
        """
        if action not in self.supported_actions():
            raise ValueError(
                f"{self.protocol()} does not support action {action.value!r}",
            )
        self.last_snapshot = None
        snapshot = self.resolve_snapshot(
            backend,
            market_name=market_name,
            action=action,
            params=params,
        )
        self.last_snapshot = snapshot
        tx_builder = TransactionBuilder(
            EvalContext(last_block_slot=current_slot(backend)),
        )
        self.contribute(action, tx_builder, snapshot=snapshot, params=params)
        tx_cbor = assemble_unsigned(tx_builder)
        try:
            return evaluate_tx_cbor(tx_cbor, snapshot.additional_utxo())
        except OgmiosEvaluationError as error:
            missing = {out_ref_text(ref) for ref in error.missing_inputs()}
            spent = sorted(missing & chosen_action_utxos(snapshot))
            if spent:
                raise ActionUtxoSpentError(spent[0]) from error
            raise
