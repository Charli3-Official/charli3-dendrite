"""Assemble a forward FluidTokens repay (full) into a `TransactionBuilder`.

A repay spends the loan UTxO with an EMPTY redeemer and drives the logic through the
loan-policy reward twin + the repay-action reward (withdraw) script; a full repay also
burns the loan NFT. The lender is paid via a wallet output carrying the repayment
receipt, the borrower-bond NFT is returned to the borrower, and a protocol fee is paid.
The config NFT + lender-bond UTxO are reference inputs; the three loan scripts are
supplied by reference.

`build_repay` contributes all of that to a caller-supplied
`pycardano.TransactionBuilder` (no balancing/signing/submission); the indices the
redeemers carry are resolved from the final canonical ordering. The decisive
correctness proof is a positive Ogmios evaluation of a forward-built repay replayed
against a captured real on-chain repay.
"""
from __future__ import annotations

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import IndefiniteList
from pycardano import MultiAsset
from pycardano import Network
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Withdrawals

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.constants import LOAN_REPAY_ACTION_SKH
from charli3_dendrite.lending.fluidtokens.transactions.context import RepaySnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import _to_utxo
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    synth_repayment_receipt,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    ActionMarkerRepay,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanPolicyMintBurnRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanPolicyWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanRepayActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import RepayData
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.utility import asset_to_value

# The FluidTokens loan-action validators cap the validity window; 360 slots mirrors the
# Danogo cap (the upper bound the validator's time checks tolerate).
_REPAY_VALIDITY_SLOTS = 360


def _ref_index(tx_builder: TransactionBuilder) -> dict[tuple[str, int], int]:
    """Map each reference input's out-ref to its index in the canonical ordering."""
    inputs = [u.input for u in tx_builder.reference_inputs if isinstance(u, UTxO)]
    inputs.sort(key=lambda i: (bytes(i.transaction_id), i.index))
    return {
        (bytes(i.transaction_id).hex(), i.index): pos for pos, i in enumerate(inputs)
    }


def _input_index(tx_builder: TransactionBuilder, out_ref: tuple[str, int]) -> int:
    """Index of `out_ref` in the canonical (tx id, index) input ordering."""
    inputs = sorted(
        tx_builder.inputs,
        key=lambda u: (bytes(u.input.transaction_id), u.input.index),
    )
    for pos, u in enumerate(inputs):
        if (bytes(u.input.transaction_id).hex(), u.input.index) == out_ref:
            return pos
    raise ValueError(f"input {out_ref} not found")


def _reward_address(script_hash: str) -> bytes:
    """The network-tagged reward (stake-script) address bytes for `script_hash`."""
    return bytes(
        Address(
            staking_part=ScriptHash(bytes.fromhex(script_hash)),
            network=Network.MAINNET,
        ),
    )


def build_repay(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RepaySnapshot,
    lender_lovelace: int,
) -> None:
    """Contribute a forward full repay to `tx_builder`.

    Wires: the loan spend (empty redeemer), the loan-NFT burn, the loan-policy reward
    twin + repay-action reward withdrawals, the config + lender-bond reference inputs,
    and the outputs (lender repayment carrying the synthesized receipt, borrower-bond
    return, protocol fee, collateral release). One repay-action redeemer + the
    loan-policy redeemers carry the config/action reference-input indices and the
    per-input action record, resolved from the final canonical ordering.

    ``lender_lovelace`` is the ADA the lender output pays (principal + accrued
    interest); the e2e sources it from the captured repay. The caller
    funds/balances/evaluates.
    """
    loan = snapshot.loan
    if loan.out_ref is None or loan.datum is None or loan.address is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref/datum/address")
    config_ref = snapshot.config.out_ref
    lender_bond_ref = snapshot.lender_bond.out_ref
    bond_ref = snapshot.borrower_bond.out_ref
    if config_ref is None or lender_bond_ref is None or bond_ref is None:
        raise ValueError("snapshot config/lender-bond/borrower-bond is missing out-ref")

    lower = (
        tx_builder.validity_start
        if tx_builder.validity_start is not None
        else tx_builder.context.last_block_slot
    )
    tx_builder.validity_start = lower
    tx_builder.ttl = lower + _REPAY_VALIDITY_SLOTS

    loan_id = snapshot.loan_id
    repay_data, repay_rdmr, policy_withdraw_rdmr, mint_rdmr = _repay_redeemers(loan_id)

    # --- spend the loan (empty redeemer) + the borrower-bond input -----------------
    tx_builder.add_script_input(
        _to_utxo(loan),
        script=_to_utxo(snapshot.spend_script_ref),
        redeemer=Redeemer(LoanSpendRedeemer()),
    )
    tx_builder.add_input(_to_utxo(snapshot.borrower_bond))

    # --- config + lender-bond reference inputs (read-only) -------------------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.lender_bond))

    # --- burn the loan NFT (-1) ----------------------------------------------------
    tx_builder.add_minting_script(
        _to_utxo(snapshot.loan_policy_script_ref),
        redeemer=Redeemer(mint_rdmr),
    )
    burn = MultiAsset(
        {
            ScriptHash(bytes.fromhex(snapshot.loan_policy)): Asset(
                {AssetName(loan_id): -1},
            ),
        },
    )
    tx_builder.mint = burn if tx_builder.mint is None else tx_builder.mint + burn

    # --- loan-policy reward twin + repay-action reward withdrawals -----------------
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.loan_policy_script_ref),
        Redeemer(policy_withdraw_rdmr),
    )
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.repay_action_script_ref),
        Redeemer(repay_rdmr),
    )
    tx_builder.withdrawals = Withdrawals(
        {
            _reward_address(snapshot.loan_policy): 0,
            _reward_address(LOAN_REPAY_ACTION_SKH): 0,
        },
    )

    # --- outputs + redeemer-index resolution from the FINAL canonical ordering -----
    lender_out, bond_return = _add_repay_outputs(
        tx_builder,
        snapshot=snapshot,
        lender_lovelace=lender_lovelace,
    )
    ref_index = _ref_index(tx_builder)
    cfg_idx = ref_index[config_ref]
    repay_rdmr.config_ref_input_index = cfg_idx
    policy_withdraw_rdmr.config_ref_input_index = cfg_idx
    mint_rdmr.config_ref_input_index = cfg_idx
    mint_rdmr.action_ref_input_index = cfg_idx
    repay_data.index_0 = _input_index(tx_builder, bond_ref)
    repay_data.index_1 = tx_builder.outputs.index(lender_out)
    repay_data.index_2 = tx_builder.outputs.index(bond_return)
    repay_data.index_3 = ref_index[lender_bond_ref]


def _repay_redeemers(
    loan_id: bytes,
) -> tuple[
    RepayData,
    LoanRepayActionWithdrawRedeemer,
    LoanPolicyWithdrawRedeemer,
    LoanPolicyMintBurnRedeemer,
]:
    """The four repay redeemers with placeholder indices (filled once ordering known).

    The repay action record is a single-loan entry; ``is_final_repayment`` True closes
    the loan. The same data objects are mutated in place after the final ordering is
    known, so every Redeemer role stays consistent.
    """
    repay_data = RepayData(
        index_0=0,
        index_1=0,
        index_2=0,
        index_3=0,
        loan_id=loan_id,
        is_final_repayment=BoolTrue(),
    )
    return (
        repay_data,
        LoanRepayActionWithdrawRedeemer(
            config_ref_input_index=0,
            actions_for_each_input=IndefiniteList([repay_data]),
        ),
        LoanPolicyWithdrawRedeemer(
            config_ref_input_index=0,
            action_marker=ActionMarkerRepay(),
        ),
        LoanPolicyMintBurnRedeemer(
            config_ref_input_index=0,
            action_marker=ActionMarkerRepay(),
            action_ref_input_index=0,
        ),
    )


def _add_repay_outputs(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RepaySnapshot,
    lender_lovelace: int,
) -> tuple[TransactionOutput, TransactionOutput]:
    """Add the lender / bond-return / fee / collateral-release outputs (in that order).

    Returns the lender + bond-return outputs (the ones the repay redeemer indexes).
    """
    loan_id = snapshot.loan_id
    if snapshot.loan.out_ref is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref")
    receipt = synth_repayment_receipt(
        loan_datum=snapshot.loan_datum,
        loan_out_ref=snapshot.loan.out_ref,
        loan_id=loan_id,
        lender_bond_policy=snapshot.lender_bond_policy,
    )
    lender_out = TransactionOutput(
        Address.decode(snapshot.lender_bond.address),
        asset_to_value(Assets(lovelace=lender_lovelace)),
        datum=receipt,
    )
    tx_builder.add_output(lender_out)

    bond_return = TransactionOutput(
        Address.decode(snapshot.borrower_bond.address),
        asset_to_value(
            Assets(
                **{
                    "lovelace": snapshot.borrower_bond.lovelace,
                    snapshot.bond_policy + loan_id.hex(): 1,
                },
            ),
        ),
    )
    tx_builder.add_output(bond_return)

    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.fee_address),
            asset_to_value(Assets(lovelace=snapshot.fee_lovelace)),
        ),
    )

    collateral_release = _collateral_release_output(snapshot)
    if collateral_release is not None:
        tx_builder.add_output(collateral_release)
    return lender_out, bond_return


def _collateral_release_output(snapshot: RepaySnapshot) -> TransactionOutput | None:
    """The collateral-release output returning the loan's collateral to the borrower.

    Every native asset the loan locks except the loan NFT is collateral; it is released
    to the borrower (the bond input's wallet). Returns ``None`` when the loan locks no
    collateral. The lovelace floor is nominal -- Ogmios evaluation does not balance.
    """
    collateral: dict[str, int] = {}
    for policy, name, qty in snapshot.loan.assets:
        if policy == snapshot.loan_policy and name == snapshot.loan_id.hex():
            continue
        collateral[policy + name] = collateral.get(policy + name, 0) + qty
    if not collateral:
        return None
    collateral["lovelace"] = OUTPUT_MIN_ADA
    return TransactionOutput(
        Address.decode(snapshot.borrower_bond.address),
        asset_to_value(Assets(**collateral)),
    )
