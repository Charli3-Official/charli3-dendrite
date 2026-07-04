"""Assemble a forward FluidTokens pool-origin borrow into a `TransactionBuilder`.

A borrow spends a pool UTxO (EMPTY redeemer) and drives all logic through the pool
reward (``Withdraw``) script: it continues the pool at the same address with the SAME
datum but its principal reduced by the borrowed amount, creates a new loan UTxO (loan
NFT + locked collateral + a synthesized :class:`LoanDatum`), and mints the loan NFT plus
the borrower-bond and lender-bond NFTs (asset name = the loan id = hash of the spent
pool out-ref). The borrow re-prices the collateral via a signed oracle feed, so it also
drives the oracle reward (``Withdraw``) script -- whose redeemer carries an off-chain
signature and is replayed verbatim from the snapshot.

`build_borrow` contributes all of that to a caller-supplied
`pycardano.TransactionBuilder`
(no balancing/signing/submission); the reference-input / redeemer indices the redeemers
carry are resolved from the final canonical ordering. The decisive correctness proof is
a positive Ogmios evaluation of a forward-built borrow replayed against a captured real
on-chain one.
"""

from __future__ import annotations

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import IndefiniteList
from pycardano import MultiAsset
from pycardano import PlutusV3Script
from pycardano import RawCBOR
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import Withdrawals
from pycardano import plutus_script_hash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.transactions._common import plutus_address
from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.context import _to_utxo
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    synth_loan_datum,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BondMintRedeemer
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import LoanMintRedeemer
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import PoolBorrowAction
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    PoolWithdrawRedeemer,
)
from charli3_dendrite.utility import asset_to_value
from charli3_dendrite.utility import slot_to_posix_ms


def build_borrow(tx_builder: TransactionBuilder, *, snapshot: BorrowSnapshot) -> None:
    """Contribute a forward pool-origin borrow to `tx_builder`.

    Wires: the pool spend (empty redeemer), the loan / borrower-bond / lender-bond
    mints, the pool reward (``Borrow``) + replayed signed oracle reward withdrawals,
    the config + oracle-feed reference inputs, and the outputs (the continuing pool
    with the reduced principal + unchanged datum at index 0, the loan UTxO with the
    synthesized datum, the lender-bond output carrying its verbatim datum, the protocol
    fee, and the borrower's bond + change). The pool ``Borrow`` action + the loan-mint
    origin pointer carry the config / oracle / lender-output / redeemer indices,
    resolved from the final canonical ordering. The caller funds/balances/evaluates.
    """
    pool = snapshot.pool
    if pool.out_ref is None or pool.datum is None or pool.address is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref/datum/address")
    config_ref = snapshot.config.out_ref
    oracle_feed_ref = snapshot.oracle_feed.out_ref
    if config_ref is None or oracle_feed_ref is None:
        raise ValueError("snapshot config/oracle-feed is missing its out-ref")

    tx_builder.validity_start = snapshot.valid_from
    tx_builder.ttl = snapshot.valid_to
    lend_date = slot_to_posix_ms(snapshot.valid_to)

    loan_id = snapshot.loan_id
    action, pool_rdmr, loan_mint_rdmr, bond_mint_rdmr = _borrow_redeemers(snapshot)

    # --- spend the pool (empty redeemer) + the borrower funding/collateral inputs ----
    tx_builder.add_script_input(
        _to_utxo(pool),
        script=_to_utxo(snapshot.pool_spend_script_ref),
        redeemer=Redeemer(LoanSpendRedeemer()),
    )
    for funding in snapshot.funding:
        tx_builder.add_input(_to_utxo(funding))

    # --- config + collateral-oracle-feed reference inputs (read-only) ---------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.oracle_feed))

    # --- mint the loan NFT + the borrower-bond + the lender-bond (all +1) -----------
    tx_builder.add_minting_script(
        _to_utxo(snapshot.loan_policy_script_ref),
        redeemer=Redeemer(loan_mint_rdmr),
    )
    tx_builder.add_minting_script(
        _to_utxo(snapshot.lender_bond_policy_script_ref),
        redeemer=Redeemer(bond_mint_rdmr),
    )
    tx_builder.add_minting_script(
        _to_utxo(snapshot.borrower_bond_policy_script_ref),
        redeemer=Redeemer(bond_mint_rdmr),
    )
    mint = MultiAsset(
        {
            ScriptHash(bytes.fromhex(snapshot.loan_policy)): Asset(
                {AssetName(loan_id): 1},
            ),
            ScriptHash(bytes.fromhex(snapshot.lender_bond_policy)): Asset(
                {AssetName(loan_id): 1},
            ),
            ScriptHash(bytes.fromhex(snapshot.borrower_bond_policy)): Asset(
                {AssetName(loan_id): 1},
            ),
        },
    )
    tx_builder.mint = mint if tx_builder.mint is None else tx_builder.mint + mint

    # --- pool reward (Borrow) + replayed signed oracle reward withdrawals -----------
    oracle_skh = _skh(snapshot.oracle_script_ref)
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.pool_policy_script_ref),
        Redeemer(pool_rdmr),
    )
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.oracle_script_ref),
        Redeemer(RawPlutusData.from_cbor(snapshot.oracle_reward_cbor)),
    )
    tx_builder.withdrawals = Withdrawals(
        {
            reward_address(snapshot.pool_policy): 0,
            reward_address(oracle_skh): 0,
        },
    )

    # --- outputs (the continuing pool MUST be index 0; see check_borrow) -------------
    lender_bond_out = _add_borrow_outputs(
        tx_builder,
        snapshot=snapshot,
        lend_date=lend_date,
    )

    # --- resolve role indices from the FINAL canonical ordering ----------------------
    refs = _ref_index(tx_builder)
    cfg_idx = refs[config_ref]
    pool_rdmr.config_ref_input_index = cfg_idx
    loan_mint_rdmr.config_ref_input_index = cfg_idx
    loan_mint_rdmr.origin_withdraw_redeemer_index = _pool_reward_redeemer_index(
        snapshot,
    )
    action.output_with_lender_token_index = tx_builder.outputs.index(lender_bond_out)
    action.chosen_collateral_oracle_ref_input_index = refs[oracle_feed_ref]
    # ADA principal: retrieve_oracle_data short-circuits to a 1:1 feed and never reads
    # the principal-oracle ref input, so this index is a replayed placeholder.
    action.principal_oracle_ref_input_index = 0


def _borrow_redeemers(
    snapshot: BorrowSnapshot,
) -> tuple[PoolBorrowAction, PoolWithdrawRedeemer, LoanMintRedeemer, BondMintRedeemer]:
    """The borrow redeemers with placeholder indices (filled once ordering is known).

    The same :class:`PoolBorrowAction` object is mutated in place after the final
    ordering is known so every Redeemer role stays consistent; the borrower-bond and
    lender-bond mints share the one :class:`BondMintRedeemer` (the spent pool out-ref).
    """
    if snapshot.pool.out_ref is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref")
    action = PoolBorrowAction(
        borrower_address=plutus_address(Address.decode(snapshot.borrower_address)),
        output_with_lender_token_index=0,
        principal_oracle_ref_input_index=0,
        chosen_collateral_index=snapshot.chosen_collateral_index,
        chosen_collateral_oracle_ref_input_index=0,
        wanted_principal_amount=snapshot.principal_amount,
        pool_id=snapshot.pool_id,
        permissioned_condition_withdraw_index=(
            snapshot.permissioned_condition_withdraw_index
        ),
    )
    return (
        action,
        PoolWithdrawRedeemer(
            config_ref_input_index=0,
            actions_for_each_input=IndefiniteList([action]),
        ),
        LoanMintRedeemer(
            config_ref_input_index=0,
            is_pool_origin=BoolTrue(),
            origin_withdraw_redeemer_index=0,
        ),
        BondMintRedeemer(
            origin_input_refs=IndefiniteList(
                [
                    TxOutRef(
                        tx_id=bytes.fromhex(snapshot.pool.out_ref[0]),
                        index=snapshot.pool.out_ref[1],
                    ),
                ],
            ),
        ),
    )


def _add_borrow_outputs(
    tx_builder: TransactionBuilder,
    *,
    snapshot: BorrowSnapshot,
    lend_date: int,
) -> TransactionOutput:
    """Add the pool / loan / lender-bond / fee / borrower outputs (in that order).

    Returns the lender-bond output (the one the Borrow action indexes). The continuing
    pool is added first so it lands at absolute output index 0, matching the per-input
    index ``check_borrow`` uses for both the pool continuation and the loan output.
    """
    pool = snapshot.pool
    if pool.address is None or pool.datum is None:
        raise ValueError("snapshot pool UTxO is missing its address/datum")
    if snapshot.lender_bond_out.datum is None:
        raise ValueError("snapshot lender-bond output is missing its datum")

    pool_out = TransactionOutput(
        Address.decode(pool.address),
        asset_to_value(
            Assets(
                **{
                    "lovelace": snapshot.pool_continuation_lovelace,
                    snapshot.pool_policy + snapshot.pool_id.hex(): 1,
                },
            ),
        ),
        datum=RawCBOR(bytes.fromhex(pool.datum)),
    )
    tx_builder.add_output(pool_out)

    loan_datum = synth_loan_datum(
        pool_datum=snapshot.pool_datum,
        pool_id=snapshot.pool_id,
        principal_amount=snapshot.principal_amount,
        lend_date=lend_date,
        chosen_collateral_index=snapshot.chosen_collateral_index,
    )
    loan_out = TransactionOutput(
        Address.decode(snapshot.loan_address),
        asset_to_value(
            Assets(
                **{
                    "lovelace": snapshot.loan_lovelace,
                    snapshot.loan_policy + snapshot.loan_id.hex(): 1,
                    snapshot.collateral_unit: snapshot.collateral_amount,
                },
            ),
        ),
        datum=loan_datum,
    )
    tx_builder.add_output(loan_out)

    lender_bond_out = TransactionOutput(
        Address.decode(snapshot.lender_bond_out.address),
        asset_to_value(
            Assets(
                **{
                    "lovelace": snapshot.lender_bond_out.lovelace,
                    snapshot.lender_bond_policy + snapshot.loan_id.hex(): 1,
                },
            ),
        ),
        datum=RawCBOR(bytes.fromhex(snapshot.lender_bond_out.datum)),
    )
    tx_builder.add_output(lender_bond_out)

    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.fee_address),
            asset_to_value(Assets(lovelace=snapshot.fee_lovelace)),
        ),
    )

    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.borrower_address),
            asset_to_value(
                Assets(
                    **{
                        "lovelace": snapshot.borrower_output_lovelace,
                        snapshot.borrower_bond_policy + snapshot.loan_id.hex(): 1,
                    },
                ),
            ),
        ),
    )
    return lender_bond_out


def _ref_index(tx_builder: TransactionBuilder) -> dict[tuple[str, int], int]:
    """Map each reference input's out-ref to its index in the canonical ordering."""
    from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index

    return ref_index(tx_builder)


def _pool_reward_redeemer_index(snapshot: BorrowSnapshot) -> int:
    """Position of the pool reward in the script context's canonical redeemer ordering.

    Plutus V3 orders the redeemers map by script purpose (Minting < Spending <
    Rewarding) and then by key. The borrow mints three policies, spends one pool input,
    and rewards the pool + oracle scripts, so the pool reward sits after the three mints
    and the one spend, at its rank among the byte-sorted reward credentials.
    """
    n_mint = 3
    n_spend = 1
    reward_hashes = sorted([snapshot.pool_policy, _skh(snapshot.oracle_script_ref)])
    return n_mint + n_spend + reward_hashes.index(snapshot.pool_policy)


def _skh(script_ref: Utxo) -> str:
    """The script hash (hex) of a resolved reference-script UTxO."""
    if script_ref.ref_script is None:
        raise ValueError("reference UTxO carries no script")
    return plutus_script_hash(
        PlutusV3Script(bytes.fromhex(script_ref.ref_script)),
    ).payload.hex()
