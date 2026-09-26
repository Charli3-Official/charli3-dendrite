"""FluidTokens V4 borrower-action redeemers as pycardano PlutusData (byte-exact).

Mirrors ``lib/fluidtokens/types/{pool,loan}.ak`` of ``ft-cardano-loans-v4`` at
``a8bb3f4d``. Layouts V4 kept from V3 are re-exported from the V3 module: the empty
spend redeemer, the loan mint and loan withdraw (dispatch) redeemers, the loan action
constructors (V4 ``Action`` keeps V3's ``ActionType`` numbering), the change-collateral
action data and the bond mint redeemer.

V4 splits every pool action into a dispatch withdraw (:class:`PoolWithdrawRedeemer`,
one action) plus a per-action withdraw script; the borrow action carries one
:class:`BorrowData` per spent pool. Repay and recast data drop V3's lender-bond
reference-input fields.

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

__all__ = [
    "ActionTypeChangeCollateral",
    "AssetManagerMintRedeemer",
    "ActionTypeRecast",
    "ActionTypeRepay",
    "BondMintRedeemer",
    "BoolFalse",
    "BoolTrue",
    "BorrowData",
    "ChangeCollateralData",
    "LoanChangeCollateralActionWithdrawRedeemer",
    "LoanMintRedeemer",
    "LoanRecastActionWithdrawRedeemer",
    "LoanRepayActionWithdrawRedeemer",
    "LoanSpendRedeemer",
    "LoanWithdrawRedeemer",
    "PoolActionBorrow",
    "PoolBorrowActionWithdrawRedeemer",
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
class PoolActionBorrow(PlutusData):
    """Pool ``Action.Borrow`` == Constr1[] (``Cancel`` is Constr0)."""

    CONSTR_ID = 1


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
