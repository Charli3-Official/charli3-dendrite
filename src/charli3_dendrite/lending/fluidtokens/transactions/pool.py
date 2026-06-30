"""Assemble forward FluidTokens pool create / cancel into a builder.

A pool is a script UTxO (the pool spend address) carrying a pool NFT + the lender's
liquidity and an inline :class:`PoolDatum`; a borrower later draws a loan from it
(``Borrow``, already implemented).

* **Create** mints one pool NFT -- whose asset name is ``0x00`` ++ ``blake2b_224`` of
  the chosen spent input's out-ref -- and locks it + the liquidity + the pool datum at
  the pool spend address. Only the pool mint policy runs; the pool datum is the lender's
  choice (validated only when the pool is later spent), so it is carried through
  verbatim.
* **Cancel** spends the pool UTxO (empty redeemer via the pool spend script), burns the
  pool NFT, and drives the pool-policy reward (``Cancel``) authorized by the lender's
  signature (its verification-key hash is added as a required signer). The liquidity
  returns to the lender.

`build_create_pool` / `build_cancel_pool` contribute to a caller-supplied
`pycardano.TransactionBuilder` (no balancing/signing/submission); the config
reference-input index each redeemer carries is resolved from the final canonical
ordering.
"""

from __future__ import annotations

import hashlib

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import IndefiniteList
from pycardano import MultiAsset
from pycardano import RawCBOR
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import VerificationKeyHash
from pycardano import Withdrawals

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.context import CancelPoolSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import CreatePoolSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.context import _to_utxo
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import PoolCancelAction
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    PoolCancelWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import PoolMintRedeemer
from charli3_dendrite.utility import asset_to_value


def pool_nft_name(input_ref: tuple[str, int], index: int = 0) -> bytes:
    """The pool NFT asset name minted from `input_ref` at mint `index`.

    The pool mint policy derives it as the one-byte mint index followed by
    ``blake2b_224`` of the chosen input out-ref's serialized Plutus ``OutputReference``.
    """
    out_ref = TxOutRef(tx_id=bytes.fromhex(input_ref[0]), index=input_ref[1])
    digest = hashlib.blake2b(out_ref.to_cbor(), digest_size=28).digest()
    return bytes([index]) + digest


def build_create_pool(
    tx_builder: TransactionBuilder,
    *,
    snapshot: CreatePoolSnapshot,
) -> None:
    """Contribute a forward create-pool to `tx_builder`.

    Wires: the lender funding inputs, the pool-NFT mint (its asset name derived from
    the chosen input out-ref), the config reference input, and the pool output (the
    pool NFT + liquidity + the inline pool datum at the pool spend address). The caller
    funds/balances/evaluates.
    """
    config_ref = snapshot.config.out_ref
    if config_ref is None:
        raise ValueError("snapshot config is missing its out-ref")

    name = pool_nft_name(snapshot.input_ref)
    mint_rdmr = PoolMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(
            tx_id=bytes.fromhex(snapshot.input_ref[0]),
            index=snapshot.input_ref[1],
        ),
    )

    for funding in snapshot.funding:
        tx_builder.add_input(_to_utxo(funding))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))

    tx_builder.add_minting_script(
        _to_utxo(snapshot.pool_policy_script_ref),
        redeemer=Redeemer(mint_rdmr),
    )
    mint = MultiAsset(
        {ScriptHash(bytes.fromhex(snapshot.pool_policy)): Asset({AssetName(name): 1})},
    )
    tx_builder.mint = mint if tx_builder.mint is None else tx_builder.mint + mint

    pool_value = {
        "lovelace": snapshot.pool_lovelace,
        snapshot.pool_policy + name.hex(): 1,
    }
    for policy, asset_name, qty in snapshot.liquidity:
        pool_value[policy + asset_name] = qty
    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.pool_address),
            asset_to_value(Assets(**pool_value)),
            datum=RawCBOR(bytes.fromhex(snapshot.pool_datum)),
        ),
    )

    mint_rdmr.config_ref_input_index = ref_index(tx_builder)[config_ref]


def build_cancel_pool(
    tx_builder: TransactionBuilder,
    *,
    snapshot: CancelPoolSnapshot,
) -> None:
    """Contribute a forward cancel-pool to `tx_builder`.

    Wires: the pool spend (empty redeemer), the pool-NFT burn, the pool-policy reward
    (``Cancel``) withdrawal, the config reference input, and the lender required signer
    (its ``lenderAuth`` verification-key hash). The liquidity release is left to the
    caller's change handling. The caller funds/balances/evaluates.
    """
    pool = snapshot.pool
    if pool.out_ref is None or pool.datum is None or pool.address is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref/datum/address")
    config_ref = snapshot.config.out_ref
    if config_ref is None:
        raise ValueError("snapshot config is missing its out-ref")

    pool_id = snapshot.pool_id
    mint_rdmr = PoolMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(
            tx_id=bytes.fromhex(snapshot.mint_input_ref[0]),
            index=snapshot.mint_input_ref[1],
        ),
    )
    withdraw_rdmr = PoolCancelWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=IndefiniteList([PoolCancelAction(pool_id=pool_id)]),
    )

    # --- spend the pool (empty redeemer) + the lender funding inputs -----------------
    tx_builder.add_script_input(
        _to_utxo(pool),
        script=_to_utxo(snapshot.pool_spend_script_ref),
        redeemer=Redeemer(LoanSpendRedeemer()),
    )
    for funding in snapshot.funding:
        tx_builder.add_input(_to_utxo(funding))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))

    # --- burn the pool NFT (-1) ------------------------------------------------------
    tx_builder.add_minting_script(
        _to_utxo(snapshot.pool_policy_script_ref),
        redeemer=Redeemer(mint_rdmr),
    )
    burn = MultiAsset(
        {
            ScriptHash(bytes.fromhex(snapshot.pool_policy)): Asset(
                {AssetName(pool_id): -1},
            ),
        },
    )
    tx_builder.mint = burn if tx_builder.mint is None else tx_builder.mint + burn

    # --- pool-policy reward (Cancel) + the lender required signer --------------------
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.pool_policy_script_ref),
        Redeemer(withdraw_rdmr),
    )
    tx_builder.withdrawals = Withdrawals({reward_address(snapshot.pool_policy): 0})
    tx_builder.required_signers = [VerificationKeyHash(snapshot.lender_pkh)]

    cfg_idx = ref_index(tx_builder)[config_ref]
    mint_rdmr.config_ref_input_index = cfg_idx
    withdraw_rdmr.config_ref_input_index = cfg_idx
