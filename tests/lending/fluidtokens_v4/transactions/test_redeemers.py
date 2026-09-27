"""Every redeemer of the captured V4 transactions round-trips through its class."""

from __future__ import annotations

import pytest
from pycardano import PlutusData

from charli3_dendrite.lending.fluidtokens.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions import redeemers as r
from tests.lending.fluidtokens_v4.transactions.replay import fixture

CAPTURES = [
    "borrow_single",
    "borrow_multi",
    "repay_single",
    "repay_multi",
    "change_collateral_single",
    "change_collateral_multi",
    "pool_create",
    "pool_create_token",
    "pool_edit",
    "pool_edit_deposit",
    "pool_cancel",
    "claim",
    "claim_bond_first",
]

# The lender manager's WithdrawBonds action, named by its config.
_WITHDRAW_BONDS = LenderManagerConfigDatum.from_cbor(
    next(
        u["datum"]
        for u in fixture("claim")["ref_inputs"]
        if any(p == c.LENDER_MANAGER_CONFIG_NFT_POLICY for p, _, _ in u["assets"])
    ),
).withdraw_bonds_action_script_hash.hex()

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
    ("spend", c.POOL_MANAGER_SPEND_SKH): r.LoanSpendRedeemer,
    ("mint", c.POOL_POLICY): r.PoolMintRedeemer,
    ("mint", c.POOL_MANAGER_POLICY): r.PoolManagerMintRedeemer,
    ("reward", c.POOL_MANAGER_POLICY): r.PoolManagerWithdrawRedeemer,
    ("reward", c.POOL_EDIT_ACTION_SKH): r.PoolEditActionWithdrawRedeemer,
    ("reward", c.POOL_CANCEL_ACTION_SKH): r.PoolCancelWithdrawRedeemer,
    (
        "reward",
        c.POOL_MANAGER_EDIT_POOL_ACTION_SKH,
    ): r.PoolManagerActionWithdrawRedeemer,
    (
        "reward",
        c.POOL_MANAGER_CANCEL_ACTION_SKH,
    ): r.PoolManagerActionWithdrawRedeemer,
    ("spend", c.LENDER_MANAGER_SPEND_SKH): r.LoanSpendRedeemer,
    ("spend", c.ASSET_MANAGER_SPEND_SKH): r.LoanSpendRedeemer,
    ("reward", c.LENDER_MANAGER_WITHDRAW_SKH): r.LenderManagerWithdrawRedeemer,
    ("reward", c.REPAYMENT_POLICY): r.AssetManagerWithdrawRedeemer,
    # The WithdrawBonds action reads no redeemer; FluidTokens sends the empty one.
    ("reward", _WITHDRAW_BONDS): r.LoanSpendRedeemer,
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
    assert len(_typed_redeemers()) == 69  # noqa: PLR2004


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


def test_pool_action_redeemer_layouts() -> None:
    name = b"\x00" + b"\xaa" * 28
    names = "9f581d00" + "aa" * 28 + "ff"
    assert r.PoolWithdrawRedeemer(0, r.PoolActionEdit()).to_cbor().hex() == (
        "d8799f00d87d80ff"
    )
    assert r.PoolWithdrawRedeemer(1, r.PoolActionCancel()).to_cbor().hex() == (
        "d8799f01d87980ff"
    )
    assert (
        r.PoolManagerWithdrawRedeemer(0, r.PoolManagerActionEditPool()).to_cbor().hex()
        == "d8799f00d87c80ff"
    )
    assert (
        r.PoolManagerWithdrawRedeemer(1, r.PoolManagerActionCancel()).to_cbor().hex()
        == "d8799f01d87980ff"
    )
    assert r.PoolManagerMintRedeemer(1, 5).to_cbor().hex() == "d8799f0105ff"
    assert r.PoolManagerActionWithdrawRedeemer(0, 4, [name]).to_cbor().hex() == (
        "d8799f0004" + names + "ff"
    )
    assert (
        r.PoolEditActionWithdrawRedeemer(0, [r.PoolEditData(name)]).to_cbor().hex()
        == "d8799f009fd8799f581d00" + "aa" * 28 + "ffffff"
    )


def test_the_pool_manager_policy_carries_both_owner_checks() -> None:
    # The owner checks are parameters applied to the pool-manager policy script.
    refs = [utxo_from_dict(u) for u in fixture("pool_create")["ref_inputs"]]
    script = bytes.fromhex(script_ref_by_hash(refs, c.POOL_MANAGER_POLICY).ref_script)
    assert bytes.fromhex(c.POOL_MANAGER_EDIT_POOL_ACTION_SKH) in script
    assert bytes.fromhex(c.POOL_MANAGER_CANCEL_ACTION_SKH) in script


def test_lender_manager_redeemer_layouts() -> None:
    assert (
        r.LenderManagerWithdrawRedeemer(4, r.LenderManagerActionWithdrawBonds())
        .to_cbor()
        .hex()
        == "d8799f04d87980ff"
    )
    assert r.AssetManagerWithdrawRedeemer(3).to_cbor().hex() == "d8799f03ff"


def test_the_lender_manager_spend_script_carries_its_dispatch() -> None:
    # The dispatch is a parameter applied to the lender-manager spend script.
    refs = [utxo_from_dict(u) for u in fixture("claim")["ref_inputs"]]
    script = bytes.fromhex(
        script_ref_by_hash(refs, c.LENDER_MANAGER_SPEND_SKH).ref_script,
    )
    assert bytes.fromhex(c.LENDER_MANAGER_WITHDRAW_SKH) in script
