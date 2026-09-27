"""FluidTokens V4 action redeemers as pycardano PlutusData (byte-exact).

Mirrors ``lib/fluidtokens/types/{pool,pool_manager,loan}.ak`` of
``ft-cardano-loans-v4`` at ``a8bb3f4d``. Layouts V4 kept from V3 are re-exported from
the V3 module: the empty spend redeemer, the loan mint and loan withdraw (dispatch)
redeemers, the loan action constructors (V4 ``Action`` keeps V3's ``ActionType``
numbering), the change-collateral action data, the bond mint redeemer, the pool mint
redeemer, and the pool-cancel list (V4's ``PoolCancelActionWithdrawRedeemer`` of
``CancelData`` has the layout of V3's :class:`PoolCancelWithdrawRedeemer` of
:class:`PoolCancelAction`).

V4 splits every pool action into a dispatch withdraw (:class:`PoolWithdrawRedeemer`,
one action) plus a per-action withdraw script; the borrow action carries one
:class:`BorrowData` per spent pool. A pool edit or cancel also runs the pool
manager: its dispatch withdraw (:class:`PoolManagerWithdrawRedeemer`) names the
owner-check script, whose redeemer lists the pool NFT names. Repay and recast data
drop V3's lender-bond reference-input fields. A lender's claim runs the lender-manager
dispatch (:class:`LenderManagerWithdrawRedeemer`) and the asset-manager withdraw
(:class:`AssetManagerWithdrawRedeemer`).

Every list field is an untyped ``IndefiniteList`` rebuilt element by element in
``__post_init__``: that is how the contracts' serialiser encodes them, and decoded raw
entries would otherwise re-serialise with definite-length bodies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeVar

from pycardano import Datum
from pycardano import IndefiniteList
from pycardano import PlutusData

from charli3_dendrite.lending.fluidtokens.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    ActionTypeChangeCollateral,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import ActionTypeRecast
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import ActionTypeRepay
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BondMintRedeemer
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BoolFalse
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    ChangeCollateralData,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanChangeCollateralActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import LoanMintRedeemer
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import PoolCancelAction
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    PoolCancelWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import PoolMintRedeemer

__all__ = [
    "ActionTypeChangeCollateral",
    "AssetManagerMintRedeemer",
    "AssetManagerWithdrawRedeemer",
    "ActionTypeRecast",
    "ActionTypeRepay",
    "BondMintRedeemer",
    "BoolFalse",
    "BoolTrue",
    "BorrowData",
    "ChangeCollateralData",
    "LenderManagerActionWithdrawBonds",
    "LenderManagerWithdrawRedeemer",
    "LoanChangeCollateralActionWithdrawRedeemer",
    "LoanMintRedeemer",
    "LoanRecastActionWithdrawRedeemer",
    "LoanRepayActionWithdrawRedeemer",
    "LoanSpendRedeemer",
    "LoanWithdrawRedeemer",
    "PoolActionBorrow",
    "PoolActionCancel",
    "PoolActionEdit",
    "PoolBorrowActionWithdrawRedeemer",
    "PoolCancelAction",
    "PoolCancelWithdrawRedeemer",
    "PoolEditActionWithdrawRedeemer",
    "PoolEditData",
    "PoolManagerActionCancel",
    "PoolManagerActionEditPool",
    "PoolManagerActionWithdrawRedeemer",
    "PoolManagerMintRedeemer",
    "PoolManagerWithdrawRedeemer",
    "PoolMintRedeemer",
    "PoolWithdrawRedeemer",
    "RecastData",
    "RepayData",
]

_P = TypeVar("_P", bound=PlutusData)


def typed_indefinite(items: Iterable, cls: type[_P]) -> IndefiniteList:
    """``items`` as an ``IndefiniteList`` of ``cls``, decoding raw entries."""
    return IndefiniteList(
        [item if isinstance(item, cls) else cls.from_primitive(item) for item in items],
    )


@dataclass
class PoolActionCancel(PlutusData):
    """Pool ``Action.Cancel`` == Constr0[]."""

    CONSTR_ID = 0


@dataclass
class PoolActionBorrow(PlutusData):
    """Pool ``Action.Borrow`` == Constr1[]."""

    CONSTR_ID = 1


@dataclass
class PoolActionEdit(PlutusData):
    """Pool ``Action.Edit`` == Constr4[]."""

    CONSTR_ID = 4


@dataclass
class PoolWithdrawRedeemer(PlutusData):
    """Pool dispatch withdraw redeemer == ``PoolWithdrawRedeemer`` in pool.ak.

    == Constr0[config_ref_input_index, action]. One redeemer covers every pool the
    transaction spends; the per-action withdraw script checks each pool.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    action: Datum


@dataclass
class BorrowData(PlutusData):
    """One pool's borrow terms == ``BorrowData`` in pool.ak.

    ``borrower_address`` is a Plutus address; the output indexes are absolute; an
    oracle reference-input index is ignored by the contract when that asset is not
    oracle-priced; ``pool_id`` is the pool NFT asset name;
    ``permissioned_condition_withdraw_index`` is ignored by permissionless pools.
    """

    CONSTR_ID = 0
    borrower_address: Datum
    output_with_lender_token_index: int
    output_with_borrower_token_index: int
    principal_oracle_ref_input_index: int
    chosen_collateral_index: int
    chosen_collateral_oracle_ref_input_index: int
    wanted_principal_amount: int
    pool_id: bytes
    permissioned_condition_withdraw_index: int


@dataclass
class PoolBorrowActionWithdrawRedeemer(PlutusData):
    """Borrow-action withdraw redeemer: one :class:`BorrowData` per spent pool.

    == Constr0[config_ref_input_index, IndefiniteList[BorrowData]], in the order the
    pools appear among the transaction's sorted inputs.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`BorrowData`."""
        self.actions_for_each_input = typed_indefinite(
            self.actions_for_each_input,
            BorrowData,
        )


@dataclass
class RepayData(PlutusData):
    """One loan's repayment == ``RepayData`` in loan.ak.

    ``is_final_repayment`` (Plutus ``Bool``) closes a perpetual loan.
    """

    CONSTR_ID = 0
    borrower_bond_output_index: int
    loan_id: bytes
    is_final_repayment: Datum


@dataclass
class LoanRepayActionWithdrawRedeemer(PlutusData):
    """Repay-action withdraw redeemer: one :class:`RepayData` per spent loan."""

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`RepayData`."""
        self.actions_for_each_input = typed_indefinite(
            self.actions_for_each_input,
            RepayData,
        )


@dataclass
class RecastData(PlutusData):
    """One loan's recast == ``RecastData`` in loan.ak (amount in principal units)."""

    CONSTR_ID = 0
    borrower_bond_output_index: int
    amount_paid: int
    loan_id: bytes


@dataclass
class LoanRecastActionWithdrawRedeemer(PlutusData):
    """Recast-action withdraw redeemer: one :class:`RecastData` per spent loan."""

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`RecastData`."""
        self.actions_for_each_input = typed_indefinite(
            self.actions_for_each_input,
            RecastData,
        )


@dataclass
class AssetManagerMintRedeemer(PlutusData):
    """Repayment-receipt mint redeemer == ``AssetManagerMintRedeemer``.

    ``loan_withdraw_redeemer_index`` is the loan dispatch withdraw's position in the
    transaction's redeemers; the claim index is read only by a claim. ``input_ref`` is
    not read by the policy.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    input_ref: TxOutRef
    loan_withdraw_redeemer_index: int
    loan_claim_action_withdraw_redeemer_index: int


@dataclass
class PoolEditData(PlutusData):
    """One pool's edit == ``EditData`` in pool.ak: the pool NFT name."""

    CONSTR_ID = 0
    pool_id: bytes


@dataclass
class PoolEditActionWithdrawRedeemer(PlutusData):
    """Edit-action withdraw redeemer: one :class:`PoolEditData` per spent pool.

    == Constr0[config_ref_input_index, IndefiniteList[PoolEditData]], in the order the
    pools appear among the transaction's sorted inputs.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`PoolEditData`."""
        self.actions_for_each_input = typed_indefinite(
            self.actions_for_each_input,
            PoolEditData,
        )


@dataclass
class PoolManagerMintRedeemer(PlutusData):
    """Pool-manager policy mint and burn redeemer == ``PoolManagerMintRedeemer``.

    ``pool_withdraw_redeemer_index`` is the pool dispatch withdraw's position in the
    transaction's redeemers; the policy reads it only when it burns.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    pool_withdraw_redeemer_index: int


@dataclass
class PoolManagerActionCancel(PlutusData):
    """Pool-manager ``Action.CancelPoolManager`` == Constr0[]."""

    CONSTR_ID = 0


@dataclass
class PoolManagerActionEditPool(PlutusData):
    """Pool-manager ``Action.EditPool`` == Constr3[]."""

    CONSTR_ID = 3


@dataclass
class PoolManagerWithdrawRedeemer(PlutusData):
    """Pool-manager dispatch withdraw redeemer == ``PoolManagerWithdrawRedeemer``.

    == Constr0[config_ref_input_index, action]. The dispatch reads no config; the
    index is filled like every other config index.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    action: Datum


@dataclass
class PoolManagerActionWithdrawRedeemer(PlutusData):
    """Owner-check withdraw redeemer of a pool-manager edit or cancel.

    == ``CancelPoolManagerActionWithdrawRedeemer``, which both actions use:
    Constr0[config_ref_input_index, pool_withdraw_redeemer_index, names]. ``names[i]``
    is the NFT name of the i-th spent pool and of the i-th spent pool manager, each in
    the transaction's sorted input order; ``pool_withdraw_redeemer_index`` is the pool
    dispatch withdraw's position in the transaction's redeemers.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    pool_withdraw_redeemer_index: int
    pool_manager_nft_names: IndefiniteList

    def __post_init__(self) -> None:
        """Keep the names an ``IndefiniteList`` of bytes."""
        self.pool_manager_nft_names = IndefiniteList(
            [bytes(name) for name in self.pool_manager_nft_names],
        )


@dataclass
class AssetManagerWithdrawRedeemer(PlutusData):
    """Asset-manager withdraw redeemer == ``AssetManagerWithdrawRedeemer``.

    == Constr0[config_ref_input_index]. The asset manager releases a payment whose
    owner token is among the transaction's inputs.
    """

    CONSTR_ID = 0
    config_ref_input_index: int


@dataclass
class LenderManagerActionWithdrawBonds(PlutusData):
    """Lender-manager ``LenderManagerAction.WithdrawBonds`` == Constr0[]."""

    CONSTR_ID = 0


@dataclass
class LenderManagerWithdrawRedeemer(PlutusData):
    """Lender-manager dispatch withdraw redeemer == ``LenderManagerWithdrawRedeemer``.

    == Constr0[config_ref_input_index, action]; the index points at the lender-manager
    config, not the protocol config.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    action: Datum
