"""FluidTokens loan-action redeemers must round-trip byte-exact.

Every captured redeemer in ``fixtures/redeemers.json`` is decoded with its matching
dataclass and re-serialized; the bytes must match the on-chain capture exactly. A
constructive test also builds a redeemer from scratch to prove the builder path (not
just decode) produces correct bytes.
"""

import json
from pathlib import Path

from pycardano import IndefiniteList

from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanChangeCollateralActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanRecastActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanRepayActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import RepayData
from charli3_dendrite.lending.transactions.base import LendingAction

FIX = json.loads((Path(__file__).parent / "fixtures" / "redeemers.json").read_text())


def _roundtrip(cls, cbor: str) -> None:
    assert cls.from_cbor(bytes.fromhex(cbor)).to_cbor_hex() == cbor


def test_loan_spend_roundtrips():
    cbor = FIX["loan_spend"]["cbor"]
    assert cbor == "d87980"
    _roundtrip(LoanSpendRedeemer, cbor)


def test_repay_single_roundtrips():
    _roundtrip(LoanRepayActionWithdrawRedeemer, FIX["repay"]["single"]["cbor"])


def test_repay_multi_roundtrips():
    _roundtrip(LoanRepayActionWithdrawRedeemer, FIX["repay"]["multi"]["cbor"])


def test_change_collateral_add_roundtrips():
    _roundtrip(
        LoanChangeCollateralActionWithdrawRedeemer,
        FIX["change_collateral"]["add"]["cbor"],
    )


def test_change_collateral_two_roundtrips():
    _roundtrip(
        LoanChangeCollateralActionWithdrawRedeemer,
        FIX["change_collateral"]["two"]["cbor"],
    )


def test_recast_single_roundtrips():
    _roundtrip(LoanRecastActionWithdrawRedeemer, FIX["recast"]["single"]["cbor"])


def test_recast_two_roundtrips():
    _roundtrip(LoanRecastActionWithdrawRedeemer, FIX["recast"]["two"]["cbor"])


def test_repay_fresh_construct_matches_capture():
    # Build from scratch (not decode) and reproduce the captured repay.single bytes,
    # proving the builder path yields byte-exact CBOR.
    rdmr = LoanRepayActionWithdrawRedeemer(
        config_ref_input_index=3,
        actions_for_each_input=IndefiniteList(
            [
                RepayData(
                    borrower_bond_output_index=1,
                    lender_bond_ref_input_index=0,
                    lender_bond_ref_input_policy_id_index=1,
                    lender_bond_ref_input_asset_name_index=0,
                    loan_id=bytes.fromhex(
                        "c83f7c948fa39af8adabefb00bb3d68f829f3018d1e25830ffbc9a08",
                    ),
                    is_final_repayment=BoolTrue(),
                ),
            ],
        ),
    )
    assert rdmr.to_cbor_hex() == FIX["repay"]["single"]["cbor"]


def test_lending_action_enum_extended():
    assert LendingAction.RECAST == "recast"
    assert LendingAction.REQUEST_CREATE == "request_create"
    assert LendingAction.REQUEST_CANCEL == "request_cancel"
