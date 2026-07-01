"""Assemble a forward FluidTokens request-fill (``Lend``) into a `TransactionBuilder`.

A lender fills a borrower's request UTxO: it spends the request UTxO (empty redeemer via
the request-spend ``general_spend`` script) and drives all logic through the
request-policy reward (``Withdraw`` / ``Lend``). It burns the request NFT and mints the
loan NFT + borrower-bond + lender-bond (asset name = the loan id = hash of the spent
request out-ref), creating a loan UTxO (loan NFT + the request's collateral unchanged +
a synthesized :class:`LoanDatum`) and a borrower output (the principal + the borrower
bond + ``InlineDatum(requestRef)``). The lender bond is unconstrained on chain and rides
in the tx change.

Targets the simplest case: permissionless request, ADA principal, static pricing -- so
no oracle reference inputs / oracle reward witnesses are needed. `build_lend`
contributes to a caller-supplied `pycardano.TransactionBuilder` (no
balancing/signing/submission); the config reference-input index and the loan-mint origin
pointer are resolved from the final canonical ordering. The decisive correctness proof
is a positive Ogmios evaluation of a forward-built fill replayed against a captured real
on-chain one.
"""

from __future__ import annotations

import hashlib

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import IndefiniteList
from pycardano import MultiAsset
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import Withdrawals

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import _to_utxo
from charli3_dendrite.lending.fluidtokens.transactions.context import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    synth_loan_datum_from_request,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BondMintRedeemer
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import BoolFalse
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import LoanMintRedeemer
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    RequestLendAction,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    RequestMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    RequestWithdrawRedeemer,
)
from charli3_dendrite.utility import asset_to_value
from charli3_dendrite.utility import slot_to_posix_ms

# The request-fill redeemer ordering is fixed: 4 mint policies (request burn, loan,
# lender bond, borrower bond) < 1 spend (the request UTxO via general_spend) < 1 reward
# (the request policy Lend). So the loan-mint origin pointer -- the index of the request
# reward in the Plutus V3 canonical redeemer list (Minting < Spending < Rewarding) --
# is 4 + 1 = 5. (Confirmed against the captured on-chain loan-mint redeemer.)
_REQUEST_REWARD_REDEEMER_INDEX = 5


def loan_nft_name(request_out_ref: tuple[str, int]) -> bytes:
    """The loan / bond NFT asset name minted from the spent request out-ref.

    The loan + bond policies derive it as ``blake2b_224`` of the serialized Plutus
    ``OutputReference`` of the spent request UTxO (``hash_output_ref`` in the contract)
    -- with no index prefix (unlike the request NFT, which prefixes the mint index).
    """
    out_ref = TxOutRef(
        tx_id=bytes.fromhex(request_out_ref[0]),
        index=request_out_ref[1],
    )
    return hashlib.blake2b(out_ref.to_cbor(), digest_size=28).digest()


def build_lend(tx_builder: TransactionBuilder, *, snapshot: LendSnapshot) -> None:
    """Contribute a forward request-fill (``Lend``) to `tx_builder`.

    Wires: the request spend (empty redeemer via the request-spend script), the lender
    funding inputs, the request-NFT burn + the loan / borrower-bond / lender-bond mints,
    the request-policy ``Lend`` reward, the config reference input, and the outputs (the
    borrower output at absolute index 0 -- principal + borrower bond + the request
    out-ref as inline datum -- then the loan UTxO with the synthesized datum). The loan
    ``Lend`` action carries the (inert) oracle placeholders + the chosen principal +
    request id; the loan-mint origin pointer + the config index are resolved from the
    final canonical ordering. The lender bond rides in the caller's change; the caller
    funds/balances/evaluates.
    """
    request = snapshot.request
    if request.out_ref is None or request.datum is None or request.address is None:
        raise ValueError("snapshot request UTxO is missing its out-ref/datum/address")
    config_ref = snapshot.config.out_ref
    if config_ref is None:
        raise ValueError("snapshot config is missing its out-ref")

    tx_builder.validity_start = snapshot.valid_from
    tx_builder.ttl = snapshot.valid_to
    lend_date = slot_to_posix_ms(snapshot.valid_to)

    request_id = snapshot.request_id
    loan_id = snapshot.loan_id
    request_ref = TxOutRef(
        tx_id=bytes.fromhex(request.out_ref[0]),
        index=request.out_ref[1],
    )

    lend_action = RequestLendAction(
        principal_oracle_ref_input_index=snapshot.principal_oracle_ref_input_index,
        collateral_oracle_ref_input_index=snapshot.collateral_oracle_ref_input_index,
        given_principal_amount=snapshot.given_principal_amount,
        request_id=request_id,
        permissioned_condition_withdraw_index=(
            snapshot.permissioned_condition_withdraw_index
        ),
    )
    withdraw_rdmr = RequestWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=IndefiniteList([lend_action]),
    )
    mint_rdmr = RequestMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(
            tx_id=bytes.fromhex(snapshot.mint_input_ref[0]),
            index=snapshot.mint_input_ref[1],
        ),
    )
    loan_mint_rdmr = LoanMintRedeemer(
        config_ref_input_index=0,
        is_pool_origin=BoolFalse(),
        origin_withdraw_redeemer_index=0,
    )
    bond_mint_rdmr = BondMintRedeemer(
        origin_input_refs=IndefiniteList([request_ref]),
    )

    # --- spend the request (empty redeemer) + the lender funding inputs --------------
    tx_builder.add_script_input(
        _to_utxo(request),
        script=_to_utxo(snapshot.request_spend_script_ref),
        redeemer=Redeemer(LoanSpendRedeemer()),
    )
    for funding in snapshot.funding:
        tx_builder.add_input(_to_utxo(funding))
        snapshot.add_actor_additional_utxo(ogmios_entry(funding))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))

    # --- burn the request NFT (-1) + mint loan / borrower-bond / lender-bond (+1) ----
    tx_builder.add_minting_script(
        _to_utxo(snapshot.request_policy_script_ref),
        redeemer=Redeemer(mint_rdmr),
    )
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
            ScriptHash(bytes.fromhex(snapshot.request_policy)): Asset(
                {AssetName(request_id): -1},
            ),
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

    # --- request-policy reward (Lend) ------------------------------------------------
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.request_policy_script_ref),
        Redeemer(withdraw_rdmr),
    )
    tx_builder.withdrawals = Withdrawals({reward_address(snapshot.request_policy): 0})

    # --- outputs: the borrower output MUST be absolute index 0 (see check_lend) ------
    _add_lend_outputs(
        tx_builder,
        snapshot=snapshot,
        lend_date=lend_date,
        request_ref=request_ref,
    )

    # --- resolve role indices from the FINAL canonical ordering ----------------------
    cfg_idx = ref_index(tx_builder)[config_ref]
    withdraw_rdmr.config_ref_input_index = cfg_idx
    mint_rdmr.config_ref_input_index = cfg_idx
    loan_mint_rdmr.config_ref_input_index = cfg_idx
    loan_mint_rdmr.origin_withdraw_redeemer_index = _REQUEST_REWARD_REDEEMER_INDEX


def _add_lend_outputs(
    tx_builder: TransactionBuilder,
    *,
    snapshot: LendSnapshot,
    lend_date: int,
    request_ref: TxOutRef,
) -> None:
    """Add the borrower output (absolute index 0) then the loan output.

    ``check_lend`` reads the borrower output at ``self.outputs[index]`` (index 0 for a
    single fill) and the loan output as the first output on the loan smart credential,
    so the borrower output is added first. The borrower output pays the principal + the
    borrower bond and carries the request out-ref as its inline datum (2 flattened
    assets for ADA principal); the loan output holds the loan NFT + the request's
    collateral (unchanged) + the synthesized :class:`LoanDatum`.
    """
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
            datum=request_ref,
        ),
    )

    loan_datum = synth_loan_datum_from_request(
        request_datum=snapshot.request_datum,
        request_id=snapshot.request_id,
        given_principal_amount=snapshot.given_principal_amount,
        lend_date=lend_date,
    )
    tx_builder.add_output(
        TransactionOutput(
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
        ),
    )
