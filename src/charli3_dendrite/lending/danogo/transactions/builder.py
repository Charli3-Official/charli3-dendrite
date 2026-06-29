"""Danogo implementation of the lending transaction seam."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import TransactionBuilder

from charli3_dendrite.lending.danogo.transactions.build import add_actor_funding
from charli3_dendrite.lending.danogo.transactions.build import add_borrower_funding
from charli3_dendrite.lending.danogo.transactions.build import add_increase_funding
from charli3_dendrite.lending.danogo.transactions.build import (
    add_modify_collateral_funding,
)
from charli3_dendrite.lending.danogo.transactions.build import add_repay_funding
from charli3_dendrite.lending.danogo.transactions.build import build_create_loan
from charli3_dendrite.lending.danogo.transactions.build import build_increase_loan
from charli3_dendrite.lending.danogo.transactions.build import build_modify_collateral
from charli3_dendrite.lending.danogo.transactions.build import build_repay
from charli3_dendrite.lending.danogo.transactions.build import build_topup_withdraw
from charli3_dendrite.lending.danogo.transactions.build import safe_borrow_amount
from charli3_dendrite.lending.danogo.transactions.build import (
    safe_collateral_for_modify,
)
from charli3_dendrite.lending.danogo.transactions.build import safe_increase_amount
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import IncreaseLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import (
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.transactions.base import AbstractLendingTxBuilder
from charli3_dendrite.lending.transactions.base import ActionParams
from charli3_dendrite.lending.transactions.base import LendingAction

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot


class DanogoTxBuilder(AbstractLendingTxBuilder):
    """Builds Danogo create / deposit / withdraw / repay / increase / modify txs."""

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "Danogo"

    @classmethod
    def supported_actions(cls) -> set[LendingAction]:
        """Actions: create-loan, deposit/withdraw, repay, increase, modify."""
        return {
            LendingAction.BORROW,
            LendingAction.DEPOSIT,
            LendingAction.WITHDRAW,
            LendingAction.REPAY,
            LendingAction.INCREASE,
            LendingAction.MODIFY_COLLATERAL,
        }

    def resolve_snapshot(
        self,
        backend: AbstractBackend,
        *,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """Resolve the live snapshot the action needs.

        BORROW resolves a `CreateLoanSnapshot`; DEPOSIT/WITHDRAW resolve a shared
        `TopupWithdrawSnapshot`; REPAY resolves a `RepaySnapshot`, INCREASE an
        `IncreaseLoanSnapshot`, and MODIFY_COLLATERAL a `ModifyCollateralSnapshot`,
        each for the specific open loan named by `params.loan_utxo`.
        """
        if action == LendingAction.BORROW:
            return CreateLoanSnapshot.from_backend(backend, market_name=market_name)
        if action in (LendingAction.DEPOSIT, LendingAction.WITHDRAW):
            return TopupWithdrawSnapshot.from_backend(backend, market_name=market_name)
        if action == LendingAction.REPAY:
            if params.loan_utxo is None:
                raise ValueError("REPAY requires params.loan_utxo")
            return RepaySnapshot.from_backend(
                backend,
                market_name=market_name,
                loan_utxo=params.loan_utxo,
            )
        if action == LendingAction.INCREASE:
            if params.loan_utxo is None:
                raise ValueError("INCREASE requires params.loan_utxo")
            return IncreaseLoanSnapshot.from_backend(
                backend,
                market_name=market_name,
                loan_utxo=params.loan_utxo,
            )
        if action == LendingAction.MODIFY_COLLATERAL:
            if params.loan_utxo is None:
                raise ValueError("MODIFY_COLLATERAL requires params.loan_utxo")
            return ModifyCollateralSnapshot.from_backend(
                backend,
                market_name=market_name,
                loan_utxo=params.loan_utxo,
            )
        raise NotImplementedError(f"resolve_snapshot for {action}")

    def contribute(
        self,
        action: LendingAction,
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Wire the action's spend / mint(-burn) / outputs / redeemers + funding."""
        if action == LendingAction.BORROW:
            self._contribute_borrow(tx_builder, snapshot=snapshot, params=params)
        elif action in (LendingAction.DEPOSIT, LendingAction.WITHDRAW):
            self._contribute_topup_withdraw(
                action,
                tx_builder,
                snapshot=snapshot,
                params=params,
            )
        elif action == LendingAction.REPAY:
            self._contribute_repay(tx_builder, snapshot=snapshot, params=params)
        elif action == LendingAction.INCREASE:
            self._contribute_increase(tx_builder, snapshot=snapshot, params=params)
        elif action == LendingAction.MODIFY_COLLATERAL:
            self._contribute_modify_collateral(
                tx_builder,
                snapshot=snapshot,
                params=params,
            )
        else:
            raise NotImplementedError(f"contribute for {action}")

    @staticmethod
    def _contribute_borrow(
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Wire a create-loan (BORROW): safe-borrow, build, fund the borrower."""
        assert isinstance(snapshot, CreateLoanSnapshot)  # noqa: S101
        if params.actor_utxo is None:
            raise ValueError("BORROW requires params.actor_utxo")
        actor = Address.decode(params.actor_address)
        # Derive a safe borrow from the live collateral value, contribute the
        # create-loan components, then fund it from the borrower input. The borrower
        # additional-utxo entry is stashed on the snapshot so the seam's generic
        # `build_and_evaluate` feeds Ogmios the exact same `additionalUtxo` set.
        borrow = safe_borrow_amount(snapshot, params.collateral, params.borrow_amount)
        build_create_loan(
            tx_builder,
            snapshot=snapshot,
            actor_address=actor,
            collateral=params.collateral,
            borrow_amount=borrow,
        )
        snapshot.add_actor_additional_utxo(
            add_borrower_funding(
                tx_builder,
                snapshot=snapshot,
                actor=actor,
                actor_utxo=params.actor_utxo,
                collateral=params.collateral,
            ),
        )

    @staticmethod
    def _contribute_topup_withdraw(
        action: LendingAction,
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Wire a supply DEPOSIT / WITHDRAW: direction from the action, build, fund."""
        assert isinstance(snapshot, TopupWithdrawSnapshot)  # noqa: S101
        if params.actor_utxo is None:
            raise ValueError(f"{action.value} requires params.actor_utxo")
        actor = Address.decode(params.actor_address)
        # Direction comes from the action; `params.amount` is the supply-token
        # magnitude. The actor funds the action (the deposited supply token, or the
        # dTokens it burns to withdraw) from its input; that funding input is the sole
        # `additionalUtxo` Ogmios cannot resolve, so it is stashed on the snapshot for
        # the seam's generic `build_and_evaluate`.
        supply_change = (
            params.amount if action == LendingAction.DEPOSIT else -params.amount
        )
        build_topup_withdraw(
            tx_builder,
            snapshot=snapshot,
            actor_address=actor,
            supply_change=supply_change,
        )
        if action == LendingAction.DEPOSIT:
            funding = {snapshot.market_info.supply_token: params.amount}
        else:
            funding = {snapshot.pool_skh + snapshot.market_name: params.amount}
        snapshot.add_actor_additional_utxo(
            add_actor_funding(
                tx_builder,
                actor=actor,
                actor_utxo=params.actor_utxo,
                funding=funding,
            ),
        )

    @staticmethod
    def _contribute_repay(
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Wire a REPAY (full / partial / collateral edit): build then fund."""
        assert isinstance(snapshot, RepaySnapshot)  # noqa: S101
        if params.loan_utxo is None:
            raise ValueError("REPAY requires params.loan_utxo")
        if params.actor_utxo is None:
            raise ValueError("REPAY requires params.actor_utxo")
        actor = Address.decode(params.actor_address)
        # `params.amount` is the requested repayment (the redeemer's
        # `pool_change_amount`); `build_repay` clamps it to the loan's accrued debt for
        # a full repay. `params.collateral` (if set) is the TARGET absolute collateral
        # the loan output should lock after a partial repay; an empty mapping leaves the
        # loan's current collateral unchanged. The borrower funds the supply token to
        # repay (and any ADDED collateral) and proves loan ownership with the owner NFT;
        # that funding input is the sole `additionalUtxo` Ogmios cannot resolve, so it
        # is stashed on the snapshot for the seam's generic `build_and_evaluate`.
        collateral = params.collateral or None
        build_repay(
            tx_builder,
            snapshot=snapshot,
            amount=params.amount,
            collateral=collateral,
        )
        snapshot.add_actor_additional_utxo(
            add_repay_funding(
                tx_builder,
                snapshot=snapshot,
                actor=actor,
                actor_utxo=params.actor_utxo,
                amount=params.amount,
                collateral=collateral,
            ),
        )

    @staticmethod
    def _contribute_increase(
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Wire an INCREASE: HF/util preflight, build, fund the borrower (owner NFT)."""
        assert isinstance(snapshot, IncreaseLoanSnapshot)  # noqa: S101
        if params.loan_utxo is None:
            raise ValueError("INCREASE requires params.loan_utxo")
        if params.actor_utxo is None:
            raise ValueError("INCREASE requires params.actor_utxo")
        if params.borrow_amount is None:
            raise ValueError("INCREASE requires params.borrow_amount")
        actor = Address.decode(params.actor_address)
        # `params.borrow_amount` is the additional supply token to borrow against the
        # loan. Preflight the post-increase health factor + utilization cap against the
        # live oracle prices, contribute the increase components, then fund the borrower
        # input (owner NFT only; the pool pays the supply out). The funding input is the
        # sole `additionalUtxo` Ogmios cannot resolve, so it is stashed on the snapshot
        # for the seam's generic `build_and_evaluate`.
        safe_increase_amount(snapshot, borrow_amount=params.borrow_amount)
        build_increase_loan(
            tx_builder,
            snapshot=snapshot,
            borrow_amount=params.borrow_amount,
        )
        snapshot.add_actor_additional_utxo(
            add_increase_funding(
                tx_builder,
                snapshot=snapshot,
                actor=actor,
                actor_utxo=params.actor_utxo,
            ),
        )

    @staticmethod
    def _contribute_modify_collateral(
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Wire a MODIFY_COLLATERAL: HF preflight, build, fund the borrower."""
        assert isinstance(snapshot, ModifyCollateralSnapshot)  # noqa: S101
        if params.loan_utxo is None:
            raise ValueError("MODIFY_COLLATERAL requires params.loan_utxo")
        if params.actor_utxo is None:
            raise ValueError("MODIFY_COLLATERAL requires params.actor_utxo")
        if not params.collateral:
            raise ValueError("MODIFY_COLLATERAL requires params.collateral")
        actor = Address.decode(params.actor_address)
        # `params.collateral` is the TARGET absolute collateral the loan output should
        # lock; the loan debt is unchanged. Preflight the post-modification health
        # factor against the loan's current accrued debt (interest advanced from the
        # pool reference datum), contribute the modify components, then fund the
        # borrower input (owner NFT + any added collateral). The funding input is the
        # sole `additionalUtxo` Ogmios cannot resolve, so it is stashed on the snapshot
        # for the seam's generic `build_and_evaluate`.
        safe_collateral_for_modify(snapshot, target_collateral=params.collateral)
        build_modify_collateral(
            tx_builder,
            snapshot=snapshot,
            target_collateral=params.collateral,
        )
        snapshot.add_actor_additional_utxo(
            add_modify_collateral_funding(
                tx_builder,
                snapshot=snapshot,
                actor=actor,
                actor_utxo=params.actor_utxo,
                target_collateral=params.collateral,
            ),
        )
