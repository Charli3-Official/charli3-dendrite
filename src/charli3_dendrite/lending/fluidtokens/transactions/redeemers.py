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
class RepayData(PlutusData):
    """Per-input repay action == Constr0([...]).

    Six fields: four leading plain ``int`` indices, the 28-byte ``loan_id``, and the
    ``is_final_repayment`` flag (``Constr0[]`` False / ``Constr1[]`` True). The exact
    output/input/ref-input semantics of the index fields are confirmed in the action
    builders (Tasks 9-11); they are left neutrally named here.
    """

    CONSTR_ID = 0
    index_0: int
    index_1: int
    index_2: int
    index_3: int
    loan_id: bytes
    is_final_repayment: PlutusBool


@dataclass
class ChangeCollateralData(PlutusData):
    """Per-input change-collateral action == Constr0([...]).

    Five fields: a leading plain ``int`` index, ``new_collateral_amount`` (the target
    locked collateral), the 28-byte ``loan_id``, then two trailing plain ``int``
    indices. The exact index semantics are confirmed in the action builders
    (Tasks 9-11); those fields are left neutrally named here.
    """

    CONSTR_ID = 0
    index_0: int
    new_collateral_amount: int
    loan_id: bytes
    index_1: int
    index_2: int


@dataclass
class RecastData(PlutusData):
    """Per-input recast action == Constr0([...]).

    Six fields: four leading plain ``int`` indices, ``amount_paid``, and the 28-byte
    ``loan_id``. The exact index semantics are confirmed in the action builders
    (Tasks 9-11); those fields are left neutrally named here.
    """

    CONSTR_ID = 0
    index_0: int
    index_1: int
    index_2: int
    index_3: int
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
