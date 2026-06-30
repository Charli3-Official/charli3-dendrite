"""Assemble a forward FluidTokens change-collateral into a `TransactionBuilder`.

Change-collateral spends the loan UTxO (empty redeemer) and re-creates it at the same
address with the SAME datum but a NEW locked-collateral amount, returning the
borrower-bond NFT. Nothing is minted/burned. The action re-prices the collateral via a
signed oracle feed, so it also drives the oracle reward (``Withdraw``) script -- whose
redeemer carries an off-chain signature and is replayed verbatim from the snapshot
rather than synthesized.

`build_change_collateral` contributes all of that to a caller-supplied
`pycardano.TransactionBuilder` (no balancing/signing/submission); the reference-input /
input indices the redeemers carry are resolved from the final canonical ordering. The
decisive correctness proof is a positive Ogmios evaluation of a forward-built
change-collateral replayed against a captured real on-chain one.
"""
from __future__ import annotations

from pycardano import Address
from pycardano import IndefiniteList
from pycardano import RawCBOR
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import Withdrawals

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.transactions._common import input_index
from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions._common import (
    set_validity_window,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.context import _to_utxo
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    ActionMarkerChangeCollateral,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    ChangeCollateralData,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanChangeCollateralActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanPolicyWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.utility import asset_to_value


def _collateral_unit(snapshot: ChangeCollateralSnapshot) -> str:
    """The loan's single collateral asset unit (the non-loan-NFT asset it locks)."""
    for policy, name, _qty in snapshot.loan.assets:
        if policy != snapshot.loan_policy:
            return policy + name
    raise ValueError("loan UTxO locks no collateral asset")


def build_change_collateral(
    tx_builder: TransactionBuilder,
    *,
    snapshot: ChangeCollateralSnapshot,
    target_collateral: int,
) -> None:
    """Contribute a forward change-collateral to `tx_builder`.

    Wires: the loan spend (empty redeemer), the loan-policy reward twin + the
    change-collateral action reward + the replayed signed oracle reward withdrawals, the
    config + oracle-feed reference inputs, and the outputs (the continuing loan with the
    new collateral amount + unchanged datum, and the borrower-bond return). The
    change-collateral action record carries the loan input index, the new collateral
    amount, the loan id, and the oracle-feed reference index, resolved from the final
    canonical ordering. ``target_collateral`` is the new locked amount of the loan's
    collateral asset. The caller funds/balances/evaluates.
    """
    loan = snapshot.loan
    if loan.out_ref is None or loan.datum is None or loan.address is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref/datum/address")
    config_ref = snapshot.config.out_ref
    oracle_feed_ref = snapshot.oracle_feed.out_ref
    if config_ref is None or oracle_feed_ref is None:
        raise ValueError("snapshot config/oracle-feed is missing its out-ref")

    set_validity_window(tx_builder)

    cc_data = ChangeCollateralData(
        index_0=0,
        new_collateral_amount=target_collateral,
        loan_id=snapshot.loan_id,
        index_1=0,
        index_2=0,
    )
    action_rdmr = LoanChangeCollateralActionWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=IndefiniteList([cc_data]),
    )
    policy_rdmr = LoanPolicyWithdrawRedeemer(
        config_ref_input_index=0,
        action_marker=ActionMarkerChangeCollateral(),
    )

    # --- spend the loan (empty redeemer) + the borrower-bond input -----------------
    tx_builder.add_script_input(
        _to_utxo(loan),
        script=_to_utxo(snapshot.spend_script_ref),
        redeemer=Redeemer(LoanSpendRedeemer()),
    )
    tx_builder.add_input(_to_utxo(snapshot.borrower_bond))

    # --- config + oracle-feed reference inputs (read-only) -------------------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.oracle_feed))

    # --- loan-policy reward twin + action reward + replayed signed oracle reward ----
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.loan_policy_script_ref),
        Redeemer(policy_rdmr),
    )
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.action_script_ref),
        Redeemer(action_rdmr),
    )
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.oracle_script_ref),
        Redeemer(RawPlutusData.from_cbor(snapshot.oracle_reward_cbor)),
    )
    tx_builder.withdrawals = Withdrawals(
        {
            reward_address(snapshot.loan_policy): 0,
            reward_address(_skh(snapshot.action_script_ref)): 0,
            reward_address(_skh(snapshot.oracle_script_ref)): 0,
        },
    )

    # --- outputs: continuing loan (new collateral, same datum) + bond return -------
    loan_out = TransactionOutput(
        Address.decode(loan.address),
        asset_to_value(
            Assets(
                **{
                    "lovelace": loan.lovelace,
                    snapshot.loan_policy + snapshot.loan_id.hex(): 1,
                    _collateral_unit(snapshot): target_collateral,
                },
            ),
        ),
        datum=RawCBOR(bytes.fromhex(loan.datum)),
    )
    tx_builder.add_output(loan_out)
    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.borrower_bond.address),
            asset_to_value(
                Assets(
                    **{
                        "lovelace": OUTPUT_MIN_ADA,
                        snapshot.bond_policy + snapshot.loan_id.hex(): 1,
                    },
                ),
            ),
        ),
    )

    # --- resolve role indices from the FINAL canonical ordering --------------------
    refs = ref_index(tx_builder)
    cfg_idx = refs[config_ref]
    policy_rdmr.config_ref_input_index = cfg_idx
    action_rdmr.config_ref_input_index = cfg_idx
    cc_data.index_0 = input_index(tx_builder, loan.out_ref)
    cc_data.index_1 = refs[oracle_feed_ref]


def _skh(script_ref: Utxo) -> str:
    """The script hash (hex) of a resolved reference-script UTxO."""
    from pycardano import PlutusV3Script
    from pycardano import plutus_script_hash

    if script_ref.ref_script is None:
        raise ValueError("reference UTxO carries no script")
    return plutus_script_hash(
        PlutusV3Script(bytes.fromhex(script_ref.ref_script)),
    ).payload.hex()
