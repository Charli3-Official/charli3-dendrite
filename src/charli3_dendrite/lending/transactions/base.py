"""Protocol-agnostic lending transaction seam.

Mirrors the AMM `AbstractPoolState.swap_utxo` pattern: `build_and_evaluate` is concrete
orchestration; `resolve_snapshot` and `contribute` are the per-protocol primitives. No
signing/submission — the success criterion is a positive Ogmios evaluation.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any

from pycardano import TransactionBuilder

from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from charli3_dendrite.lending.transactions.infra import current_slot
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot


class LendingAction(str, Enum):
    """A supported lending action."""

    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    BORROW = "borrow"
    REPAY = "repay"
    INCREASE = "increase"
    MODIFY_COLLATERAL = "modify_collateral"
    RECAST = "recast"
    REQUEST_CREATE = "request_create"
    REQUEST_CANCEL = "request_cancel"
    LEND = "lend"
    POOL_CREATE = "pool_create"
    POOL_CANCEL = "pool_cancel"


@dataclass
class ActionParams:
    """Inputs for a lending action. Fields are populated per action kind.

    DEPOSIT/WITHDRAW use `actor_address` + `amount` (magnitude of supply-token change;
    direction comes from the action). BORROW uses `actor_address` + `collateral` +
    `borrow_amount`. REPAY uses `actor_address` + `loan_utxo` (the loan out-ref being
    repaid) + `amount` (the repay magnitude / `pool_change_amount`). INCREASE (borrow
    more against an existing loan) uses `actor_address` + `loan_utxo` (the loan out-ref
    to increase) + `borrow_amount` (the additional supply token to borrow).
    MODIFY_COLLATERAL (add/remove collateral on an existing loan, no repay) uses
    `actor_address` + `loan_utxo` (the loan out-ref to modify) + `collateral` (the
    TARGET absolute collateral the loan output should lock). `actor_utxo` optionally
    pins the funding input out-ref.

    For BORROW, `collateral` is the absolute collateral the new loan locks. For REPAY,
    `collateral` is the TARGET absolute collateral amounts the loan output should lock
    after the (partial) repay -- optional, and an empty mapping leaves the loan's
    current collateral unchanged. A collateral target is only valid on a partial repay
    (a full repay closes the loan and releases all collateral). For MODIFY_COLLATERAL,
    `collateral` is the TARGET absolute collateral the loan output should lock (the
    same semantics as the repay target), and the loan debt is unchanged.
    """

    actor_address: str
    amount: int = 0
    collateral: dict[str, int] = field(default_factory=dict)
    borrow_amount: int | None = None
    actor_utxo: str | None = None
    loan_utxo: str | None = None


class AbstractLendingTxBuilder(ABC):
    """Build + Ogmios-evaluate lending transactions for one protocol."""

    @classmethod
    @abstractmethod
    def protocol(cls) -> str:
        """Protocol name (e.g. 'Danogo')."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def supported_actions(cls) -> set[LendingAction]:
        """Actions this builder can assemble."""
        raise NotImplementedError

    @abstractmethod
    def resolve_snapshot(
        self,
        backend: AbstractBackend,
        *,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """Resolve the live UTxOs/script-refs the action needs.

        `params` carries the per-action inputs; actions whose resolution depends on
        them (e.g. REPAY needs `params.loan_utxo` to locate the loan to spend) read
        them here, while actions resolving purely from market state ignore them.
        """
        raise NotImplementedError

    @abstractmethod
    def contribute(
        self,
        action: LendingAction,
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Wire spend / mint-burn / outputs / redeemers / reference inputs."""
        raise NotImplementedError

    def build_and_evaluate(
        self,
        *,
        backend: AbstractBackend,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> list[dict[str, Any]]:
        """Resolve → contribute → assemble unsigned → Ogmios evaluate."""
        if action not in self.supported_actions():
            raise ValueError(
                f"{self.protocol()} does not support action {action.value!r}",
            )
        snapshot = self.resolve_snapshot(
            backend,
            market_name=market_name,
            action=action,
            params=params,
        )
        tx_builder = TransactionBuilder(
            EvalContext(last_block_slot=current_slot(backend)),
        )
        self.contribute(action, tx_builder, snapshot=snapshot, params=params)
        tx_cbor = assemble_unsigned(tx_builder)
        return evaluate_tx_cbor(tx_cbor, snapshot.additional_utxo())
