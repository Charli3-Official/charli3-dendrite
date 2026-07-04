"""FluidTokens implementation of the lending transaction seam.

Dispatches the protocol-agnostic :class:`LendingAction` to FluidTokens' per-action
``build_*`` contributors (each takes a pre-resolved, capture-derived snapshot and wires
its spend / mint-burn / outputs / redeemers / reference inputs into a caller-supplied
``TransactionBuilder``). The decisive correctness proof for each action remains its
byte-exact + gated Ogmios e2e test; this builder is the registry-facing dispatch over
those contributors.

Live ``resolve_snapshot`` is wired for the pool actions, LEND, REPAY, and
REQUEST_CANCEL: POOL_CANCEL resolves a :class:`CancelPoolSnapshot` from the backend (the
pool out-ref + lender address come from ``ActionParams``), LEND resolves a
:class:`LendSnapshot` from the backend (the request out-ref + optional principal /
lender address come from ``ActionParams``), REPAY resolves a :class:`RepaySnapshot` from
the backend (the loan out-ref + actor address come from ``ActionParams``),
REQUEST_CANCEL resolves a :class:`CancelRequestSnapshot` from the backend (the request
out-ref + borrower address come from ``ActionParams``). POOL_CREATE, REQUEST_CREATE,
MODIFY_COLLATERAL, and BORROW cannot be resolved from ``ActionParams`` alone -- they
need typed terms or a time-bound oracle witness / injected datums / fee that
``ActionParams`` does not carry -- so callers build those snapshots via
``<Snapshot>.from_backend(...)`` and call :meth:`contribute` directly. Every supported
action is now either live-resolvable via ``resolve_snapshot`` or has such a
``from_backend`` directive.
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
from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import RecastSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import RepaySnapshot
from charli3_dendrite.lending.fluidtokens.transactions.lend import build_lend
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
    LendingAction.LEND: LendSnapshot,
    LendingAction.POOL_CREATE: CreatePoolSnapshot,
    LendingAction.POOL_CANCEL: CancelPoolSnapshot,
}


# Actions whose snapshot needs typed terms / an off-chain witness that ``ActionParams``
# cannot carry: they resolve via ``<Snapshot>.from_backend(...)`` + ``contribute``
# directly rather than through ``resolve_snapshot``. Each maps to its guidance message.
_FROM_BACKEND_DIRECTIVE = {
    LendingAction.POOL_CREATE: (
        "POOL_CREATE cannot be resolved from ActionParams: it needs typed PoolTerms "
        "(+ liquidity / lovelace) that ActionParams does not carry. Build the snapshot "
        "via CreatePoolSnapshot.from_backend(...) and call "
        "FluidTokensTxBuilder().contribute(LendingAction.POOL_CREATE, ...) directly."
    ),
    LendingAction.REQUEST_CREATE: (
        "REQUEST_CREATE cannot be resolved from ActionParams: it needs typed "
        "RequestTerms (+ collateral / lovelace) that ActionParams does not carry. "
        "Build the snapshot via CreateRequestSnapshot.from_backend(...) and call "
        "FluidTokensTxBuilder().contribute(LendingAction.REQUEST_CREATE, ...) directly."
    ),
    LendingAction.MODIFY_COLLATERAL: (
        "MODIFY_COLLATERAL cannot be resolved from ActionParams: it needs a time-bound "
        "oracle witness (oracle_reward_cbor + the oracle feed / script-ref out-refs) "
        "that ActionParams cannot carry. Build the snapshot via "
        "ChangeCollateralSnapshot.from_backend(...) and call "
        "FluidTokensTxBuilder().contribute(LendingAction.MODIFY_COLLATERAL, ..., "
        "params=ActionParams(..., amount=<target collateral>)) directly."
    ),
    LendingAction.BORROW: (
        "BORROW cannot be resolved from ActionParams: it needs a time-bound oracle "
        "witness (oracle_reward_cbor + the oracle feed / script-ref out-refs), the "
        "injected lender-bond datum preimage, and the borrow protocol fee -- none of "
        "which ActionParams can carry. Build the snapshot via "
        "BorrowSnapshot.from_backend(...) and call "
        "FluidTokensTxBuilder().contribute(LendingAction.BORROW, ...) directly."
    ),
}


# TODO(dry): hoist to a shared infra out-ref parser (also in danogo).
def _parse_out_ref(out_ref: str) -> tuple[str, int]:
    """Parse a ``tx_hash#index`` string into a ``(tx_hash, index)`` pair."""
    tx_hash, _, idx = out_ref.partition("#")
    return tx_hash, int(idx)


class FluidTokensTxBuilder(AbstractLendingTxBuilder):
    """Build FluidTokens borrow/repay/modify/recast/request/lend/pool txs."""

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "FluidTokens"

    @classmethod
    def supported_actions(cls) -> set[LendingAction]:
        """Borrow, repay, modify, recast, request, lend, and pool create/cancel."""
        return set(_SNAPSHOT_TYPE)

    def resolve_snapshot(
        self,
        backend: AbstractBackend,
        *,
        market_name: str,
        action: LendingAction,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """Resolve the live snapshot the action needs.

        POOL_CANCEL resolves a :class:`CancelPoolSnapshot` from the backend: the pool
        UTxO out-ref comes from ``params.loan_utxo`` (a ``"<txhash>#<index>"`` string)
        and the lender address from ``params.actor_address``.

        LEND resolves a :class:`LendSnapshot` from the backend: the request UTxO out-ref
        comes from ``params.loan_utxo``, the (optional) lender-supplied principal from
        ``params.amount`` (falling back to the request's ``maxPrincipal``), and the
        lender address (used to resolve the lender's funding) from
        ``params.actor_address``.

        REPAY resolves a :class:`RepaySnapshot` from the backend: the loan UTxO out-ref
        comes from ``params.loan_utxo`` and the actor (repayer) address from
        ``params.actor_address``.

        REQUEST_CANCEL resolves a :class:`CancelRequestSnapshot` from the backend: the
        request UTxO out-ref comes from ``params.loan_utxo`` and the borrower address
        from ``params.actor_address``.

        RECAST resolves a :class:`RecastSnapshot` from the backend: the loan out-ref
        comes from ``params.loan_utxo``, the borrower address from
        ``params.actor_address``, and the recast ``amount_paid`` from ``params.amount``;
        the validity window defaults from the backend tip.

        POOL_CREATE cannot be resolved from ``ActionParams`` alone -- it needs typed
        pool terms (+ liquidity / lovelace) that ``ActionParams`` does not carry -- so
        callers build the snapshot via ``CreatePoolSnapshot.from_backend(...)`` and call
        :meth:`contribute` directly.

        REQUEST_CREATE likewise cannot be resolved from ``ActionParams`` alone -- it
        needs typed request terms (+ collateral / lovelace) that ``ActionParams`` does
        not carry -- so callers build the snapshot via
        ``CreateRequestSnapshot.from_backend(...)`` and call :meth:`contribute`
        directly.

        MODIFY_COLLATERAL likewise cannot be resolved from ``ActionParams`` alone -- it
        needs a time-bound oracle witness (``oracle_reward_cbor`` + the oracle feed /
        script-ref out-refs) that ``ActionParams`` cannot carry -- so callers build the
        snapshot via ``ChangeCollateralSnapshot.from_backend(...)`` and call
        :meth:`contribute` directly (the new locked collateral amount is threaded
        through ``ActionParams.amount``).

        BORROW likewise cannot be resolved from ``ActionParams`` alone -- it needs a
        time-bound oracle witness (``oracle_reward_cbor`` + the oracle feed /
        script-ref out-refs), the injected lender-bond datum preimage, and the borrow
        protocol fee -- so callers build the snapshot via
        ``BorrowSnapshot.from_backend(...)`` and call :meth:`contribute` directly.
        """
        if action == LendingAction.POOL_CANCEL:
            if params.loan_utxo is None:
                raise ValueError("POOL_CANCEL requires params.loan_utxo")
            return CancelPoolSnapshot.from_backend(
                backend,
                pool_utxo=_parse_out_ref(params.loan_utxo),
                lender_address=params.actor_address,
            )
        if action in _FROM_BACKEND_DIRECTIVE:
            raise NotImplementedError(_FROM_BACKEND_DIRECTIVE[action])
        if action == LendingAction.LEND:
            if params.loan_utxo is None:
                raise ValueError("LEND requires params.loan_utxo (the request out-ref)")
            return LendSnapshot.from_backend(
                backend,
                request_utxo=_parse_out_ref(params.loan_utxo),
                given_principal_amount=params.amount or None,
                lender_address=params.actor_address,
            )
        if action == LendingAction.REPAY:
            if params.loan_utxo is None:
                raise ValueError("REPAY requires params.loan_utxo (the loan out-ref)")
            return RepaySnapshot.from_backend(
                backend,
                loan_utxo=_parse_out_ref(params.loan_utxo),
                actor_address=params.actor_address,
            )
        if action == LendingAction.REQUEST_CANCEL:
            if params.loan_utxo is None:
                raise ValueError(
                    "REQUEST_CANCEL requires params.loan_utxo (the request out-ref)",
                )
            return CancelRequestSnapshot.from_backend(
                backend,
                request_utxo=_parse_out_ref(params.loan_utxo),
                borrower_address=params.actor_address,
            )
        if action == LendingAction.RECAST:
            return self._resolve_recast(backend, params)
        raise NotImplementedError(
            "FluidTokens live snapshot resolution is not implemented for "
            f"{action.value}; resolve a snapshot from a captured transaction via "
            "'<Snapshot>.from_capture' and call "
            "FluidTokensTxBuilder().contribute(...) directly.",
        )

    def _resolve_recast(
        self,
        backend: AbstractBackend,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """Resolve a :class:`RecastSnapshot` from ``ActionParams``.

        The loan out-ref comes from ``params.loan_utxo``, the borrower address from
        ``params.actor_address``, and the recast ``amount_paid`` from ``params.amount``;
        the validity window defaults from the backend tip. ``amount_paid`` is a real
        magnitude (not optional-with-fallback like LEND's): omitting ``params.amount``
        (default ``0``) performs a zero-repayment recast that capitalizes the full
        outstanding debt into the new principal.
        """
        if params.loan_utxo is None:
            raise ValueError("RECAST requires params.loan_utxo (the loan out-ref)")
        if params.amount is None:
            raise ValueError(
                "RECAST requires params.amount (the recast amount_paid)",
            )
        return RecastSnapshot.from_backend(
            backend,
            loan_utxo=_parse_out_ref(params.loan_utxo),
            actor_address=params.actor_address,
            amount_paid=params.amount,
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
        REQUEST_CREATE / REQUEST_CANCEL / LEND / POOL_CREATE / POOL_CANCEL take only the
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
        elif action == LendingAction.LEND:
            build_lend(tx_builder, snapshot=cast(LendSnapshot, snapshot))
        elif action == LendingAction.POOL_CREATE:
            build_create_pool(tx_builder, snapshot=cast(CreatePoolSnapshot, snapshot))
        elif action == LendingAction.POOL_CANCEL:
            build_cancel_pool(tx_builder, snapshot=cast(CancelPoolSnapshot, snapshot))
        else:
            # `_SNAPSHOT_TYPE` admitted the action but no branch dispatches it: a new
            # entry was added without wiring it here. Fail loudly rather than fall
            # through to a mis-cast contributor.
            raise NotImplementedError(f"contribute for {action}")
