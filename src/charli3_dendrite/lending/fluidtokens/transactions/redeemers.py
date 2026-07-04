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

from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import TxOutRef


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
class BondMintRedeemer(PlutusData):
    """Borrower-/lender-bond MINT redeemer == ``BondMintRedeemer`` in bond.ak.

    == Constr0([IndefiniteList[TxOutRef]]). The bond policy derives each minted bond's
    asset name (the loan id) by hashing the listed origin input out-refs; on a
    pool-origin borrow the single entry is the spent pool UTxO's out-ref (both the
    borrower bond and the lender bond carry the identical redeemer). The field MUST stay
    an untyped ``IndefiniteList`` and ``__post_init__`` MUST rebuild each
    :class:`TxOutRef`, else pycardano re-serializes the list / nested constr
    definite-length (verified byte-exact against the captured fixture).
    """

    CONSTR_ID = 0
    origin_input_refs: IndefiniteList  # elements are TxOutRef

    def __post_init__(self) -> None:
        """Coerce decoded entries back into :class:`TxOutRef`."""
        self.origin_input_refs = IndefiniteList(
            [
                p if isinstance(p, TxOutRef) else TxOutRef.from_primitive(p)
                for p in self.origin_input_refs
            ],
        )


@dataclass
class RequestMintRedeemer(PlutusData):
    """Request-policy MINT/BURN redeemer == ``RequestMintRedeemer`` in request.ak.

    == Constr0([config_ref_input_index, input_ref]). On a CREATE the request policy
    derives the minted request NFT's asset name as ``0x<index>`` ++
    ``blake2b_224(input_ref)``, so ``input_ref`` is the chosen spent input out-ref. On a
    BURN (cancel) the policy counts only positive mints, so ``input_ref`` only needs to
    be a spent input; we reproduce the captured value for byte-exactness.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    input_ref: TxOutRef


@dataclass
class RequestCancelAction(PlutusData):
    """``RequestAction.Cancel`` == Constr0([request_id]) in request.ak.

    ``CancelAfterExpiration`` is Constr1 and ``Lend`` is Constr2 (both out of scope for
    the borrower create/cancel path); ``request_id`` is the request NFT asset name.
    """

    CONSTR_ID = 0
    request_id: bytes


@dataclass
class RequestLendAction(PlutusData):
    """``RequestAction.Lend`` == Constr2[...] in request.ak (fills a request).

    Drives the request ``Withdraw`` (reward) script that spends a borrower's request
    UTxO to originate a loan. In the static-ADA case the two oracle indices are inert
    placeholders (the validator never reads them; the observed protocol tx uses ``0``);
    ``given_principal_amount`` is the lender-chosen principal (capped by
    ``maxPrincipal`` and floored by the collateral ratio); ``request_id`` is the
    request NFT asset name;
    ``permissioned_condition_withdraw_index`` is ignored by permissionless requests
    (replayed for byte-exactness).
    """

    CONSTR_ID = 2
    principal_oracle_ref_input_index: int
    collateral_oracle_ref_input_index: int
    given_principal_amount: int
    request_id: bytes
    permissioned_condition_withdraw_index: int


def _coerce_request_action(p: object) -> PlutusData:
    """Coerce a decoded request-action primitive to its typed class by constructor.

    Already-typed actions pass through; a raw ``CBORTag`` is dispatched on its tag
    (Constr0 == 121 -> Cancel, Constr2 == 123 -> Lend). Keeps
    :class:`RequestWithdrawRedeemer` byte-exact for both the cancel and lend paths.
    """
    if isinstance(p, (RequestCancelAction, RequestLendAction)):
        return p
    tag = getattr(p, "tag", None)
    if tag == 121:  # noqa: PLR2004 - Constr0 == Cancel
        return RequestCancelAction.from_primitive(p)
    if tag == 123:  # noqa: PLR2004 - Constr2 == Lend
        return RequestLendAction.from_primitive(p)
    raise ValueError(f"unsupported RequestAction primitive: {p!r}")


@dataclass
class RequestWithdrawRedeemer(PlutusData):
    """Request reward (withdraw) redeemer == ``RequestWithdrawRedeemer`` in request.ak.

    == Constr0([config_ref_input_index, IndefiniteList[RequestAction]]). For a single
    cancel the action list is one :class:`RequestCancelAction`. See
    :class:`LoanRepayActionWithdrawRedeemer` for why the field stays an untyped
    ``IndefiniteList`` and why ``__post_init__`` rebuilds each element.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList  # elements are RequestCancelAction | RequestLendAction  # noqa: E501

    def __post_init__(self) -> None:
        """Coerce decoded entries back into their typed Cancel / Lend action classes."""
        self.actions_for_each_input = IndefiniteList(
            [_coerce_request_action(p) for p in self.actions_for_each_input],
        )


@dataclass
class PoolBorrowAction(PlutusData):
    """``PoolAction.Borrow`` == Constr1[...] in pool.ak (``Cancel`` is Constr0).

    Drives the pool ``Withdraw`` (reward) script that mints a new loan from a pool. The
    index fields are resolved from the final canonical ordering: ``borrower_address`` is
    the Plutus address the borrower bond + change return to;
    ``output_with_lender_token_index``
    points at the lender-bond output; ``principal_oracle_ref_input_index`` /
    ``chosen_collateral_oracle_ref_input_index`` are the reference-input indices of the
    principal / collateral oracle feeds (the principal index is a replayed placeholder
    for an ADA principal); ``chosen_collateral_index`` selects the pool collateral
    option; ``wanted_principal_amount`` is the borrowed principal in units; ``pool_id``
    is the pool NFT asset name; ``permissioned_condition_withdraw_index`` is ignored by
    permissionless pools (replayed for byte-exactness).
    """

    CONSTR_ID = 1
    borrower_address: Datum  # pycardano Address constr
    output_with_lender_token_index: int
    principal_oracle_ref_input_index: int
    chosen_collateral_index: int
    chosen_collateral_oracle_ref_input_index: int
    wanted_principal_amount: int
    pool_id: bytes
    permissioned_condition_withdraw_index: int


@dataclass
class PoolWithdrawRedeemer(PlutusData):
    """Pool reward (withdraw) redeemer == ``PoolWithdrawRedeemer`` in pool.ak.

    == Constr0([config_ref_input_index, IndefiniteList[PoolAction]]). For a single
    pool-origin borrow the action list is one :class:`PoolBorrowAction`. See
    :class:`LoanRepayActionWithdrawRedeemer` for why the field stays an untyped
    ``IndefiniteList`` and why ``__post_init__`` rebuilds each element.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList  # elements are PoolBorrowAction

    def __post_init__(self) -> None:
        """Coerce decoded Borrow entries back into :class:`PoolBorrowAction`."""
        self.actions_for_each_input = IndefiniteList(
            [
                (
                    p
                    if isinstance(p, PoolBorrowAction)
                    else PoolBorrowAction.from_primitive(p)
                )
                for p in self.actions_for_each_input
            ],
        )


@dataclass
class PoolMintRedeemer(PlutusData):
    """Pool-policy MINT/BURN redeemer == ``PoolMintRedeemer`` in pool.ak.

    == Constr0([config_ref_input_index, input_ref]). On a CREATE the pool policy derives
    the minted pool NFT's asset name as ``0x<index>`` ++ ``blake2b_224(input_ref)``, so
    ``input_ref`` is the chosen spent input out-ref. On a BURN (cancel) the policy
    counts only positive mints, so ``input_ref`` only needs to be a spent input; we
    reproduce the captured value for byte-exactness. Mirrors
    :class:`RequestMintRedeemer`.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    input_ref: TxOutRef


@dataclass
class PoolCancelAction(PlutusData):
    """``PoolAction.Cancel`` == Constr0([pool_id]) in pool.ak (``Borrow`` is Constr1).

    ``pool_id`` is the pool NFT asset name.
    """

    CONSTR_ID = 0
    pool_id: bytes


@dataclass
class PoolCancelWithdrawRedeemer(PlutusData):
    """Pool reward (withdraw) redeemer for a single cancel.

    == Constr0([config_ref_input_index, IndefiniteList[PoolCancelAction]]). Distinct
    from :class:`PoolWithdrawRedeemer` (which coerces to :class:`PoolBorrowAction`); the
    field stays an untyped ``IndefiniteList`` and ``__post_init__`` rebuilds each
    element as a :class:`PoolCancelAction`, mirroring :class:`RequestWithdrawRedeemer`.
    """

    CONSTR_ID = 0
    config_ref_input_index: int
    actions_for_each_input: IndefiniteList  # elements are PoolCancelAction

    def __post_init__(self) -> None:
        """Coerce decoded Cancel entries back into :class:`PoolCancelAction`."""
        self.actions_for_each_input = IndefiniteList(
            [
                (
                    p
                    if isinstance(p, PoolCancelAction)
                    else PoolCancelAction.from_primitive(p)
                )
                for p in self.actions_for_each_input
            ],
        )


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
                (
                    p
                    if isinstance(p, ChangeCollateralData)
                    else ChangeCollateralData.from_primitive(p)
                )
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
