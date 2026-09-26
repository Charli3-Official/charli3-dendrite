"""Every redeemer of the captured V4 transactions round-trips through its class."""

from __future__ import annotations

import pytest
from pycardano import PlutusData

from charli3_dendrite.lending.fluidtokens.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.transactions import redeemers as r
from tests.lending.fluidtokens_v4.transactions.replay import fixture

CAPTURES = [
    "borrow_single",
    "borrow_multi",
    "repay_single",
    "repay_multi",
    "change_collateral_single",
    "change_collateral_multi",
]

# (purpose, script hash) -> the class of that redeemer. Oracle rewards are signed
# messages replayed verbatim and have no class here.
_CLASS: dict[tuple[str, str], type[PlutusData]] = {
    ("spend", c.POOL_SPEND_SKH): r.LoanSpendRedeemer,
    ("spend", c.LOAN_SPEND_SKH): r.LoanSpendRedeemer,
    ("mint", c.LOAN_POLICY): r.LoanMintRedeemer,
    ("mint", c.LENDER_BOND_POLICY): r.BondMintRedeemer,
    ("mint", c.BORROWER_BOND_POLICY): r.BondMintRedeemer,
    ("reward", c.POOL_POLICY): r.PoolWithdrawRedeemer,
    ("reward", c.POOL_BORROW_ACTION_SKH): r.PoolBorrowActionWithdrawRedeemer,
    ("reward", c.LOAN_POLICY): r.LoanWithdrawRedeemer,
    ("reward", c.LOAN_REPAY_ACTION_SKH): r.LoanRepayActionWithdrawRedeemer,
    (
        "reward",
        c.LOAN_CHANGE_COLLATERAL_ACTION_SKH,
    ): r.LoanChangeCollateralActionWithdrawRedeemer,
}


def _typed_redeemers() -> list[tuple[str, type[PlutusData], str]]:
    out = []
    for name in CAPTURES:
        for red in fixture(name)["redeemers"]:
            cls = _CLASS.get((red["purpose"], red["script_hash"]))
            if cls is not None:
                out.append((name, cls, red["cbor"]))
    return out


@pytest.mark.parametrize(("capture", "cls", "cbor"), _typed_redeemers())
def test_captured_redeemer_round_trips(
    capture: str,
    cls: type[PlutusData],
    cbor: str,
) -> None:
    assert cls.from_cbor(cbor).to_cbor().hex() == cbor, capture


def test_every_non_oracle_redeemer_has_a_class() -> None:
    untyped = [
        red
        for name in CAPTURES
        for red in fixture(name)["redeemers"]
        if (red["purpose"], red["script_hash"]) not in _CLASS
    ]
    # Only oracle rewards, one oracle validator per priced token: STRIKE, IAG, FLDT.
    assert {red["purpose"] for red in untyped} == {"reward"}
    assert {red["script_hash"] for red in untyped} == {
        "d97da4f3f2c3757971332b9bfb3c46df18715b059e6ab0fe66ddb036",
        "81dd5229d086ea5f3a9e3a8133f2e1b7a4d0020cc67063f8c21cdc3b",
        "4a48df8eac9f3abb39bfcd15e8cc82e8f465ece322a45ed47ef7ebb9",
    }
    assert len(_typed_redeemers()) == 35  # noqa: PLR2004


def test_recast_redeemer_layout() -> None:
    redeemer = r.LoanRecastActionWithdrawRedeemer(
        config_ref_input_index=1,
        actions_for_each_input=[
            r.RecastData(borrower_bond_output_index=2, amount_paid=5, loan_id=b"\x01"),
        ],
    )
    assert redeemer.to_cbor().hex() == "d8799f019fd8799f020541" "01ffffff"


def test_receipt_mint_redeemer_layout() -> None:
    redeemer = r.AssetManagerMintRedeemer(
        config_ref_input_index=1,
        input_ref=TxOutRef(tx_id=b"\xaa" * 32, index=0),
        loan_withdraw_redeemer_index=3,
        loan_claim_action_withdraw_redeemer_index=0,
    )
    assert redeemer.to_cbor().hex() == ("d8799f01d8799f5820" + "aa" * 32 + "00ff0300ff")
