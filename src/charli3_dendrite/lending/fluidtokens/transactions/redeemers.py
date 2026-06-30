"""FluidTokens V3 loan-action redeemers as pycardano PlutusData (byte-exact).

The loan UTxO is spent with an EMPTY ``Constr0[]`` redeemer
(:class:`LoanSpendRedeemer`); all action logic rides the per-action reward
(withdraw) redeemer, which has the shape::

    Constr0[
        config_ref_input_index: int,
        actions_for_each_input: IndefiniteList[<ActionData>],
    ]

Each action kind (repay / change-collateral / recast) carries its own
``<ActionData>`` element type. Field order/shape is verified to round-trip the
on-chain CBOR (see test_redeemers).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from pycardano import Datum
from pycardano import IndefiniteList
from pycardano import PlutusData


@dataclass
class BoolFalse(PlutusData):
    """Plutus ``False`` == Constr0([]) (``d87980``)."""

    CONSTR_ID = 0


@dataclass
class BoolTrue(PlutusData):
    """Plutus ``True`` == Constr1([]) (``d87a80``)."""

    CONSTR_ID = 1


PlutusBool = Union[BoolFalse, BoolTrue]


@dataclass
class LoanSpendRedeemer(PlutusData):
    """Empty redeemer carried on the loan general_spend input (Constr0[] == d87980)."""

    CONSTR_ID = 0


@dataclass
class ActionTypeClaim(PlutusData):
    """``ActionType.Claim`` == Constr0[] (lender / liquidation action; out of scope)."""

    CONSTR_ID = 0


@dataclass
class ActionTypeRepay(PlutusData):
    """``ActionType.Repay`` == Constr1[] (``d87a80``)."""

    CONSTR_ID = 1


@dataclass
class ActionTypeChangeCollateral(PlutusData):
    """``ActionType.ChangeCollateral`` == Constr2[] (``d87b80``)."""

    CONSTR_ID = 2


@dataclass
class ActionTypeRecast(PlutusData):
    """``ActionType.Recast`` == Constr3[] (``d87c80``)."""

    CONSTR_ID = 3


@dataclass
class LoanWithdrawRedeemer(PlutusData):
    """Loan-policy reward (withdraw) redeemer == ``LoanWithdrawRedeemer`` in loan.ak.

    == Constr0([config_ref_input_index, action_type]). The loan-policy reward account is
    the orchestration twin the loan spend (and, on a full repay, the mint-burn) delegate
    to; the loan policy checks a matching per-action withdraw is present for the chosen
    ``action_type`` (``ActionTypeRepay`` -> ``d8799f03d87a80ff``,
    ``ActionTypeChangeCollateral`` -> ``d8799f04d87b80ff``).
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    action_type: Datum


@dataclass
class LoanMintRedeemer(PlutusData):
    """Loan-policy MINT redeemer == ``LoanMintRedeemer`` in loan.ak.

    == Constr0([config_ref_input_index, is_pool_origin, origin_withdraw_redeemer_index])
    (a repay-close burn -> ``d8799f03d87a8003ff``). ``is_pool_origin`` selects pool- vs
    request-origin and ``origin_withdraw_redeemer_index`` points at that origin's
    withdraw redeemer when MINTING a loan (borrow). On a BURN (repay-close) the loan
    policy counts only positive mints, so both fields are ignored; we still reproduce
    the captured values for byte-exactness.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    is_pool_origin: Datum  # PlutusBool
    origin_withdraw_redeemer_index: int


@dataclass
class RepayData(PlutusData):
    """Per-input repay action == ``RepayData`` in loan.ak (Constr0).

    Field semantics (confirmed against `ft-cardano-loans-v3` lib/loan.ak +
    loan_repay_action.ak): ``borrower_bond_output_index`` is the absolute output index
    holding the returned borrower-bond NFT; ``lender_bond_ref_input_index`` is the
    absolute reference-input index of the lender-bond UTxO; the two
    ``..._policy_id_index`` / ``..._asset_name_index`` are the positions of the
    lender-bond policy id / loan-id asset name inside that ref UTxO's on-chain value map
    (`efficient_quantity_of`); ``is_final_repayment`` (Bool) closes a perpetual loan.
    """

    CONSTR_ID = 0
    borrower_bond_output_index: int
    lender_bond_ref_input_index: int
    lender_bond_ref_input_policy_id_index: int
    lender_bond_ref_input_asset_name_index: int
    loan_id: bytes
    is_final_repayment: PlutusBool


@dataclass
class ChangeCollateralData(PlutusData):
    """Per-input change-collateral action == ``ChangeCollateralData`` in loan.ak.

    ``borrower_bond_output_index`` is the absolute output index holding the returned
    borrower-bond NFT; ``new_collateral_amount`` is the target locked collateral;
    ``collateral_oracle_ref_input_index`` / ``principal_oracle_ref_input_index`` are the
    absolute reference-input indices of the collateral / principal oracle feeds (the
    principal index is unused when the principal is ADA).
    """

    CONSTR_ID = 0
    borrower_bond_output_index: int
    new_collateral_amount: int
    loan_id: bytes
    collateral_oracle_ref_input_index: int
    principal_oracle_ref_input_index: int


@dataclass
class RecastData(PlutusData):
    """Per-input recast action == ``RecastData`` in loan.ak (Constr0).

    Same index semantics as :class:`RepayData` (borrower-bond output index, lender-bond
    ref index + value-map policy-id / asset-name positions); ``amount_paid`` is the
    recast payment in principal units.
    """

    CONSTR_ID = 0
    borrower_bond_output_index: int
    lender_bond_ref_input_index: int
    lender_bond_ref_input_policy_id_index: int
    lender_bond_ref_input_asset_name_index: int
    amount_paid: int
    loan_id: bytes


@dataclass
class LoanRepayActionWithdrawRedeemer(PlutusData):
    """Repay reward (withdraw) redeemer.

    == Constr0([cfg_idx, IndefiniteList[RepayData]]).

    ``actions_for_each_input`` MUST stay an untyped ``IndefiniteList``; a plain
    ``List[RepayData]`` makes pycardano serialize the OUTER list DEFINITE-length.
    ``__post_init__`` MUST rebuild each :class:`RepayData`, else the decoded raw
    entries re-serialize each NESTED constr with a DEFINITE-length body. Both are
    required for byte-exact reproduction (verified against the captured fixtures).
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList  # elements are RepayData

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`RepayData`."""
        self.actions_for_each_input = IndefiniteList(
            [
                p if isinstance(p, RepayData) else RepayData.from_primitive(p)
                for p in self.actions_for_each_input
            ],
        )


@dataclass
class LoanChangeCollateralActionWithdrawRedeemer(PlutusData):
    """Change-collateral reward (withdraw) redeemer.

    == Constr0([cfg_idx, IndefiniteList[ChangeCollateralData]]). See
    :class:`LoanRepayActionWithdrawRedeemer` for why the field stays an untyped
    ``IndefiniteList`` and why ``__post_init__`` rebuilds each element.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList  # elements are ChangeCollateralData

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`ChangeCollateralData`."""
        self.actions_for_each_input = IndefiniteList(
            [
                p
                if isinstance(p, ChangeCollateralData)
                else ChangeCollateralData.from_primitive(p)
                for p in self.actions_for_each_input
            ],
        )


@dataclass
class LoanRecastActionWithdrawRedeemer(PlutusData):
    """Recast reward (withdraw) redeemer.

    == Constr0([cfg_idx, IndefiniteList[RecastData]]). See
    :class:`LoanRepayActionWithdrawRedeemer` for why the field stays an untyped
    ``IndefiniteList`` and why ``__post_init__`` rebuilds each element.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList  # elements are RecastData

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`RecastData`."""
        self.actions_for_each_input = IndefiniteList(
            [
                p if isinstance(p, RecastData) else RecastData.from_primitive(p)
                for p in self.actions_for_each_input
            ],
        )
