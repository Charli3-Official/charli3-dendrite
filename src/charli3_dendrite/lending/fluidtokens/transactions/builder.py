"""FluidTokens implementation of the lending transaction seam.

Dispatches the protocol-agnostic :class:`LendingAction` to FluidTokens' per-action
``build_*`` contributors (each takes a pre-resolved, capture-derived snapshot and wires
its spend / mint-burn / outputs / redeemers / reference inputs into a caller-supplied
``TransactionBuilder``). The decisive correctness proof for each action remains its
byte-exact + gated Ogmios e2e test; this builder is the registry-facing dispatch over
those contributors.

Live ``resolve_snapshot`` (resolving pools / config NFT / oracle feeds from a backend)
is a separate milestone: FluidTokens snapshots are currently resolved from captured
on-chain transactions via each snapshot's ``from_capture``, so ``contribute`` is the
substantive seam and ``build_and_evaluate``'s live resolution path is not yet wired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast

from charli3_dendrite.lending.fluidtokens.transactions.borrow import build_borrow
from charli3_dendrite.lending.fluidtokens.transactions.change_collateral import (
    build_change_collateral,
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
from charli3_dendrite.lending.fluidtokens.transactions.pool import build_cancel_pool
from charli3_dendrite.lending.fluidtokens.transactions.pool import build_create_pool
from charli3_dendrite.lending.fluidtokens.transactions.recast import build_recast
from charli3_dendrite.lending.fluidtokens.transactions.repay import build_repay
from charli3_dendrite.lending.fluidtokens.transactions.request import (
    build_cancel_request,
)
from charli3_dendrite.lending.fluidtokens.transactions.request import (
    build_create_request,
)
from charli3_dendrite.lending.transactions.base import AbstractLendingTxBuilder
from charli3_dendrite.lending.transactions.base import LendingAction

if TYPE_CHECKING:
    from pycardano import TransactionBuilder

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.transactions.base import ActionParams
    from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot

# action -> snapshot type each contributor expects. Most contributors take only the
# snapshot; REPAY and MODIFY_COLLATERAL additionally take one numeric magnitude,
# threaded from ``ActionParams.amount`` in :meth:`FluidTokensTxBuilder.contribute`
# (REPAY: the lender repayment lovelace; MODIFY_COLLATERAL: the new locked collateral
# amount).
_SNAPSHOT_TYPE = {
    LendingAction.BORROW: BorrowSnapshot,
    LendingAction.REPAY: RepaySnapshot,
    LendingAction.MODIFY_COLLATERAL: ChangeCollateralSnapshot,
    LendingAction.RECAST: RecastSnapshot,
    LendingAction.REQUEST_CREATE: CreateRequestSnapshot,
    LendingAction.REQUEST_CANCEL: CancelRequestSnapshot,
    LendingAction.POOL_CREATE: CreatePoolSnapshot,
    LendingAction.POOL_CANCEL: CancelPoolSnapshot,
}


class FluidTokensTxBuilder(AbstractLendingTxBuilder):
    """Builds FluidTokens borrow / repay / modify / recast / request txs."""

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "FluidTokens"

    @classmethod
    def supported_actions(cls) -> set[LendingAction]:
        """Borrow, repay, modify-collateral, recast, request, and pool create/cancel."""
        return set(_SNAPSHOT_TYPE)

    def resolve_snapshot(
        self,
        backend: AbstractBackend,
        *,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """Not yet wired: FluidTokens snapshots are resolved from captured txs.

        Live resolution (pools / config NFT / oracle feeds from a backend) is a
        separate milestone. Until then, resolve a snapshot via its ``from_capture`` and
        call :meth:`contribute` directly. ``build_and_evaluate``'s generic resolve path
        raises here so callers cannot silently get an unresolved snapshot.
        """
        raise NotImplementedError(
            "FluidTokens live snapshot resolution is not implemented yet; resolve a "
            "snapshot from a captured transaction via '<Snapshot>.from_capture' and "
            "call FluidTokensTxBuilder().contribute(...) directly.",
        )

    def contribute(
        self,
        action: LendingAction,
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,
    ) -> None:
        """Dispatch the action to its FluidTokens contributor.

        Validates that `snapshot` is the type the action expects. BORROW / RECAST /
        REQUEST_CREATE / REQUEST_CANCEL / POOL_CREATE / POOL_CANCEL take only the
        snapshot; REPAY and MODIFY_COLLATERAL additionally consume ``params.amount``
        (the lender repayment lovelace, and the new locked collateral amount,
        respectively).
        """
        expected = _SNAPSHOT_TYPE.get(action)
        if expected is None:
            raise NotImplementedError(f"contribute for {action}")
        if not isinstance(snapshot, expected):
            raise TypeError(
                f"{action.value} expects {expected.__name__}, "
                f"got {type(snapshot).__name__}",
            )
        # `expected` already guards the runtime type above; the casts only narrow it for
        # the statically-typed contributor signatures.
        if action == LendingAction.BORROW:
            build_borrow(tx_builder, snapshot=cast(BorrowSnapshot, snapshot))
        elif action == LendingAction.REPAY:
            build_repay(
                tx_builder,
                snapshot=cast(RepaySnapshot, snapshot),
                lender_lovelace=params.amount,
            )
        elif action == LendingAction.MODIFY_COLLATERAL:
            build_change_collateral(
                tx_builder,
                snapshot=cast(ChangeCollateralSnapshot, snapshot),
                target_collateral=params.amount,
            )
        elif action == LendingAction.RECAST:
            build_recast(tx_builder, snapshot=cast(RecastSnapshot, snapshot))
        elif action == LendingAction.REQUEST_CREATE:
            build_create_request(
                tx_builder,
                snapshot=cast(CreateRequestSnapshot, snapshot),
            )
        elif action == LendingAction.REQUEST_CANCEL:
            build_cancel_request(
                tx_builder,
                snapshot=cast(CancelRequestSnapshot, snapshot),
            )
        elif action == LendingAction.POOL_CREATE:
            build_create_pool(tx_builder, snapshot=cast(CreatePoolSnapshot, snapshot))
        else:  # LendingAction.POOL_CANCEL
            build_cancel_pool(tx_builder, snapshot=cast(CancelPoolSnapshot, snapshot))
