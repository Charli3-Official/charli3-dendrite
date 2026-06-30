"""Assemble a forward FluidTokens perpetual recast into a `TransactionBuilder`.

A recast spends the loan UTxO (empty redeemer) and re-creates it at the same address
with an UPDATED datum (``done_recasts`` + 1, the new capitalized ``principal_amount``,
and the new ``lend_date``) and the same collateral, driving the loan-policy reward twin
+ the recast-action reward (withdraw) script. The lender is paid the recast amount via a
wallet output carrying a recast receipt, the borrower-bond NFT is returned, and a
protocol fee is paid. The config NFT + lender-bond UTxO are reference inputs; the three
loan scripts are supplied by reference. No NFT is minted/burned and (unlike
change-collateral) no oracle is consulted.

`build_recast` contributes all of that to a caller-supplied
`pycardano.TransactionBuilder` (no balancing/signing/submission); the indices the
redeemers carry are resolved from the final canonical ordering. The decisive correctness
proof is a positive Ogmios evaluation of a forward-built recast replayed against a
captured real on-chain recast.
"""
from __future__ import annotations

from pycardano import Address
from pycardano import IndefiniteList
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import Withdrawals

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.constants import LOAN_RECAST_ACTION_SKH
from charli3_dendrite.lending.fluidtokens.transactions._common import input_index
from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.context import RecastSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import _to_utxo
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    synth_recast_receipt,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    ActionMarkerRecast,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanPolicyWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanRecastActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import RecastData
from charli3_dendrite.utility import asset_to_value


def build_recast(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RecastSnapshot,
) -> None:
    """Contribute a forward recast to `tx_builder`.

    Wires: the loan spend (empty redeemer), the loan-policy reward twin +
    recast-action reward withdrawals, the config + lender-bond reference inputs, and the
    outputs (the lender recast payment carrying the synthesized receipt, the continuing
    loan with the updated datum + same collateral, the borrower-bond return, and the
    protocol fee). The recast action record carries the loan input index, the
    lender-bond reference index, the bond-return + lender output indices, the paid
    amount, and the loan id, resolved from the final canonical ordering. The caller
    funds/balances/evaluates.
    """
    loan = snapshot.loan
    if loan.out_ref is None or loan.datum is None or loan.address is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref/datum/address")
    config_ref = snapshot.config.out_ref
    lender_bond_ref = snapshot.lender_bond.out_ref
    if config_ref is None or lender_bond_ref is None:
        raise ValueError("snapshot config/lender-bond is missing its out-ref")

    # A recast derives the loan's new lend_date (and interest accrual) from the validity
    # window, so the captured bounds are replayed exactly rather than the generic cap.
    tx_builder.validity_start = snapshot.valid_from
    tx_builder.ttl = snapshot.valid_to

    recast_data = RecastData(
        index_0=0,
        index_1=0,
        index_2=0,
        index_3=0,
        amount_paid=snapshot.amount_paid,
        loan_id=snapshot.loan_id,
    )
    action_rdmr = LoanRecastActionWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=IndefiniteList([recast_data]),
    )
    policy_rdmr = LoanPolicyWithdrawRedeemer(
        config_ref_input_index=0,
        action_marker=ActionMarkerRecast(),
    )

    # --- spend the loan (empty redeemer) + the borrower-bond input -----------------
    tx_builder.add_script_input(
        _to_utxo(loan),
        script=_to_utxo(snapshot.spend_script_ref),
        redeemer=Redeemer(LoanSpendRedeemer()),
    )
    tx_builder.add_input(_to_utxo(snapshot.borrower_bond))
    tx_builder.add_input(_to_utxo(snapshot.funding))

    # --- config + lender-bond reference inputs (read-only) -------------------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.lender_bond))

    # --- loan-policy reward twin + recast-action reward withdrawals ----------------
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.loan_policy_script_ref),
        Redeemer(policy_rdmr),
    )
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.action_script_ref),
        Redeemer(action_rdmr),
    )
    tx_builder.withdrawals = Withdrawals(
        {
            reward_address(snapshot.loan_policy): 0,
            reward_address(LOAN_RECAST_ACTION_SKH): 0,
        },
    )

    # --- outputs: lender recast payment, continuing loan, bond return, fee ----------
    lender_out, bond_return = _add_recast_outputs(tx_builder, snapshot=snapshot)

    # --- resolve role indices from the FINAL canonical ordering --------------------
    refs = ref_index(tx_builder)
    cfg_idx = refs[config_ref]
    policy_rdmr.config_ref_input_index = cfg_idx
    action_rdmr.config_ref_input_index = cfg_idx
    recast_data.index_0 = input_index(tx_builder, loan.out_ref)
    recast_data.index_1 = refs[lender_bond_ref]
    recast_data.index_2 = tx_builder.outputs.index(bond_return)
    recast_data.index_3 = tx_builder.outputs.index(lender_out)


def _add_recast_outputs(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RecastSnapshot,
) -> tuple[TransactionOutput, TransactionOutput]:
    """Add lender-payment / continuing-loan / bond-return / fee outputs (in that order).

    Returns the lender-payment + bond-return outputs (the ones the recast redeemer
    indexes).
    """
    loan = snapshot.loan
    if loan.out_ref is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref")
    receipt = synth_recast_receipt(
        loan_out_ref=loan.out_ref,
        loan_id=snapshot.loan_id,
        lender_bond_policy=snapshot.lender_bond_policy,
    )
    lender_out = TransactionOutput(
        Address.decode(snapshot.lender_address),
        asset_to_value(Assets(lovelace=snapshot.amount_paid)),
        datum=receipt,
    )
    tx_builder.add_output(lender_out)

    bond_return = TransactionOutput(
        Address.decode(snapshot.borrower_bond.address),
        asset_to_value(
            Assets(
                **{
                    "lovelace": snapshot.borrower_bond.lovelace,
                    snapshot.bond_policy + snapshot.loan_id.hex(): 1,
                },
            ),
        ),
    )
    tx_builder.add_output(bond_return)

    loan_value = {"lovelace": loan.lovelace}
    for policy, name, qty in loan.assets:
        loan_value[policy + name] = qty
    tx_builder.add_output(
        TransactionOutput(
            Address.decode(loan.address),
            asset_to_value(Assets(**loan_value)),
            datum=snapshot.new_loan_datum,
        ),
    )

    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.fee_address),
            asset_to_value(Assets(lovelace=snapshot.fee_lovelace)),
        ),
    )
    return lender_out, bond_return
