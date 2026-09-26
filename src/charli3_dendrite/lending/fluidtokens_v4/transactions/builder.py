"""FluidTokens V4 implementation of the lending transaction seam.

``resolve_snapshot`` covers a single loan or pool from :class:`ActionParams`; several
pools or loans in one transaction resolve through the snapshots' ``from_backend``
directly and go through :meth:`FluidTokensV4TxBuilder.contribute` the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast

from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.state import collateral_asset_unit
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import PoolBorrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import build_borrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    build_change_collateral,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import RecastSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import build_recast
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import build_repay
from charli3_dendrite.lending.transactions.base import AbstractLendingTxBuilder
from charli3_dendrite.lending.transactions.base import LendingAction
from charli3_dendrite.lending.transactions.infra import parse_out_ref

if TYPE_CHECKING:
    from pycardano import TransactionBuilder

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.transactions.base import ActionParams
    from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot

_SNAPSHOT_TYPE: dict[LendingAction, type[PoolActionSnapshot]] = {
    LendingAction.BORROW: BorrowSnapshot,
    LendingAction.REPAY: RepaySnapshot,
    LendingAction.MODIFY_COLLATERAL: ChangeCollateralSnapshot,
    LendingAction.RECAST: RecastSnapshot,
}


def _out_ref(value: str | None, what: str) -> tuple[str, int]:
    """``tx_hash#index`` as an out-ref; raises when it is absent."""
    if value is None:
        raise ValueError(f"params.loan_utxo must name the {what} (tx_hash#index)")
    ref = parse_out_ref(value)
    return bytes(ref.transaction_id).hex(), ref.index


def _single(collateral: dict[str, int], action: LendingAction) -> tuple[str, int]:
    """The one (unit, amount) of ``params.collateral``."""
    if len(collateral) != 1:
        raise ValueError(f"{action.value} takes exactly one collateral unit")
    return next(iter(collateral.items()))


class FluidTokensV4TxBuilder(AbstractLendingTxBuilder):
    """Build FluidTokens V4 borrow, repay, change-collateral and recast transactions."""

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "FluidTokensV4"

    @classmethod
    def supported_actions(cls) -> set[LendingAction]:
        """Borrow, repay, change collateral and recast."""
        return set(_SNAPSHOT_TYPE)

    def resolve_snapshot(
        self,
        backend: AbstractBackend,
        *,
        market_name: str,  # - a V4 action is keyed by its UTxO
        action: LendingAction,
        params: ActionParams,
    ) -> PoolActionSnapshot:
        """Resolve one pool's borrow or one loan's action from ``params``.

        - BORROW: ``loan_utxo`` is the pool, ``borrow_amount`` the principal and
          ``collateral`` the one ``{unit: amount}`` to lock (``0`` locks the least
          the pool accepts; empty uses the first option; ADA is ``"lovelace"``).
          The signed prices the pool reads (collateral and principal, except ADA)
          are fetched from the FluidTokens registry.
        - REPAY: ``loan_utxo`` is the loan; it repays in full where the loan allows,
          else its next installment, at the exact amount due (``amount`` must be 0:
          the amount is computed, not chosen).
        - MODIFY_COLLATERAL: ``loan_utxo`` is the loan and ``collateral`` its one
          ``{unit: new amount}``; prices are fetched from the registry.
        - RECAST: ``loan_utxo`` is the loan and ``amount`` the principal paid.
        """
        if action == LendingAction.BORROW:
            return self._resolve_borrow(backend, params)
        if action == LendingAction.REPAY:
            if params.amount:
                raise ValueError(
                    "REPAY pays the exact amount due; params.amount must be 0",
                )
            return RepaySnapshot.from_backend(
                backend,
                loans=[_out_ref(params.loan_utxo, "loan")],
                borrower_address=params.actor_address,
            )
        if action == LendingAction.MODIFY_COLLATERAL:
            loan = _out_ref(params.loan_utxo, "loan")
            unit, amount = _single(params.collateral, action)
            snapshot = ChangeCollateralSnapshot.from_backend(
                backend,
                changes=[(loan, amount)],
                borrower_address=params.actor_address,
            )
            if snapshot.positions[0].collateral_unit != unit:
                raise ValueError(f"loan {loan} is not collateralised in {unit}")
            return snapshot
        if action == LendingAction.RECAST:
            return RecastSnapshot.from_backend(
                backend,
                recasts=[(_out_ref(params.loan_utxo, "loan"), params.amount)],
                borrower_address=params.actor_address,
            )
        raise ValueError(f"{self.protocol()} does not support action {action.value!r}")

    def _resolve_borrow(
        self,
        backend: AbstractBackend,
        params: ActionParams,
    ) -> BorrowSnapshot:
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_utxo,
        )

        pool_ref = _out_ref(params.loan_utxo, "pool")
        if not params.borrow_amount:
            raise ValueError("BORROW requires params.borrow_amount (the principal)")
        index, amount = 0, None
        if params.collateral:
            unit, wanted = _single(params.collateral, LendingAction.BORROW)
            pool = resolve_utxo(backend, pool_ref)
            units = [
                collateral_asset_unit(option)
                for option in PoolDatum.from_cbor(pool.datum or "").collateral_options
            ]
            if unit not in units:
                raise ValueError(f"pool {pool_ref} does not take {unit} as collateral")
            index, amount = units.index(unit), wanted or None
        return BorrowSnapshot.from_backend(
            backend,
            borrows=[
                PoolBorrow(
                    pool_out_ref=pool_ref,
                    principal_amount=params.borrow_amount,
                    chosen_collateral_index=index,
                    collateral_amount=amount,
                ),
            ],
            borrower_address=params.actor_address,
        )

    def contribute(
        self,
        action: LendingAction,
        tx_builder: TransactionBuilder,
        *,
        snapshot: PoolActionSnapshot,
        params: ActionParams,  # - the snapshot carries every amount
    ) -> None:
        """Add the action described by ``snapshot`` to ``tx_builder``."""
        expected = _SNAPSHOT_TYPE.get(action)
        if expected is None:
            raise ValueError(
                f"{self.protocol()} does not support action {action.value!r}",
            )
        if not isinstance(snapshot, expected):
            raise TypeError(
                f"{action.value} expects {expected.__name__}, "
                f"got {type(snapshot).__name__}",
            )
        if action == LendingAction.BORROW:
            build_borrow(tx_builder, snapshot=cast(BorrowSnapshot, snapshot))
        elif action == LendingAction.REPAY:
            build_repay(tx_builder, snapshot=cast(RepaySnapshot, snapshot))
        elif action == LendingAction.MODIFY_COLLATERAL:
            build_change_collateral(
                tx_builder,
                snapshot=cast(ChangeCollateralSnapshot, snapshot),
            )
        else:
            build_recast(tx_builder, snapshot=cast(RecastSnapshot, snapshot))
