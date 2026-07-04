"""Assemble forward FluidTokens borrow-request create / cancel into a builder.

A borrow request is a script UTxO (the request spend address) carrying a request NFT +
the borrower's collateral and an inline :class:`RequestDatum`; a lender later fills it
(``Lend``, out of scope) to open a loan.

* **Create** mints one request NFT -- whose asset name is ``0x00`` ++ ``blake2b_224``
  of the chosen spent input's out-ref -- and locks it + the collateral + the request
  datum at the request spend address. Only the request mint policy runs; the request
  datum is the borrower's choice (validated only when the request is later spent), so
  it is carried through from the snapshot.
* **Cancel** spends the request UTxO (empty redeemer via the request spend script),
  burns the request NFT, and drives the request-policy reward (``Cancel``) authorized
  by the borrower's signature (its verification-key hash is added as a required
  signer). The collateral returns to the borrower.

`build_create_request` / `build_cancel_request` contribute to a caller-supplied
`pycardano.TransactionBuilder` (no balancing/signing/submission); the config
reference-input index each redeemer carries is resolved from the final canonical
ordering. The decisive correctness proof is a positive Ogmios evaluation of a
forward-built create / cancel replayed against a captured real on-chain one.
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
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    CancelRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import (
    CreateRequestSnapshot,
)
from charli3_dendrite.lending.fluidtokens.transactions.context import _to_utxo
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    RequestCancelAction,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    RequestMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    RequestWithdrawRedeemer,
)
from charli3_dendrite.utility import asset_to_value


def request_nft_name(input_ref: tuple[str, int], index: int = 0) -> bytes:
    """The request NFT asset name minted from `input_ref` at mint `index`.

    The request mint policy derives it as the one-byte mint index followed by
    ``blake2b_224`` of the chosen input out-ref's serialized Plutus ``OutputReference``
    (``hash_output_ref`` in the contract).
    """
    out_ref = TxOutRef(tx_id=bytes.fromhex(input_ref[0]), index=input_ref[1])
    digest = hashlib.blake2b(out_ref.to_cbor(), digest_size=28).digest()
    return bytes([index]) + digest


def build_create_request(
    tx_builder: TransactionBuilder,
    *,
    snapshot: CreateRequestSnapshot,
) -> None:
    """Contribute a forward create-request to `tx_builder`.

    Wires: the borrower funding inputs, the request-NFT mint (its asset name derived
    from the chosen input out-ref), the config reference input, and the request output
    (the request NFT + collateral + the inline request datum at the request spend
    address). The caller funds/balances/evaluates.
    """
    config_ref = snapshot.config.out_ref
    if config_ref is None:
        raise ValueError("snapshot config is missing its out-ref")

    name = request_nft_name(snapshot.input_ref)
    mint_rdmr = RequestMintRedeemer(
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
        _to_utxo(snapshot.request_policy_script_ref),
        redeemer=Redeemer(mint_rdmr),
    )
    mint = MultiAsset(
        {
            ScriptHash(bytes.fromhex(snapshot.request_policy)): Asset(
                {AssetName(name): 1},
            ),
        },
    )
    tx_builder.mint = mint if tx_builder.mint is None else tx_builder.mint + mint

    request_value = {
        "lovelace": snapshot.request_lovelace,
        snapshot.request_policy + name.hex(): 1,
    }
    for policy, asset_name, qty in snapshot.collateral:
        request_value[policy + asset_name] = qty
    tx_builder.add_output(
        TransactionOutput(
            Address.decode(snapshot.request_address),
            asset_to_value(Assets(**request_value)),
            datum=RawCBOR(bytes.fromhex(snapshot.request_datum)),
        ),
    )

    mint_rdmr.config_ref_input_index = ref_index(tx_builder)[config_ref]


def build_cancel_request(
    tx_builder: TransactionBuilder,
    *,
    snapshot: CancelRequestSnapshot,
) -> None:
    """Contribute a forward cancel-request to `tx_builder`.

    Wires: the request spend (empty redeemer), the request-NFT burn, the
    request-policy reward (``Cancel``) withdrawal, the config reference input, and the
    borrower required signer (its ``borrowerAuth`` verification-key hash). The
    collateral release is left to the caller's change handling. The caller
    funds/balances/evaluates.
    """
    request = snapshot.request
    if request.out_ref is None or request.datum is None or request.address is None:
        raise ValueError("snapshot request UTxO is missing its out-ref/datum/address")
    config_ref = snapshot.config.out_ref
    if config_ref is None:
        raise ValueError("snapshot config is missing its out-ref")

    request_id = snapshot.request_id
    mint_rdmr = RequestMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(
            tx_id=bytes.fromhex(snapshot.mint_input_ref[0]),
            index=snapshot.mint_input_ref[1],
        ),
    )
    withdraw_rdmr = RequestWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=IndefiniteList(
            [RequestCancelAction(request_id=request_id)],
        ),
    )

    # --- spend the request (empty redeemer) + the borrower funding inputs ------------
    tx_builder.add_script_input(
        _to_utxo(request),
        script=_to_utxo(snapshot.request_spend_script_ref),
        redeemer=Redeemer(LoanSpendRedeemer()),
    )
    for funding in snapshot.funding:
        tx_builder.add_input(_to_utxo(funding))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.config))

    # --- burn the request NFT (-1) ---------------------------------------------------
    tx_builder.add_minting_script(
        _to_utxo(snapshot.request_policy_script_ref),
        redeemer=Redeemer(mint_rdmr),
    )
    burn = MultiAsset(
        {
            ScriptHash(bytes.fromhex(snapshot.request_policy)): Asset(
                {AssetName(request_id): -1},
            ),
        },
    )
    tx_builder.mint = burn if tx_builder.mint is None else tx_builder.mint + burn

    # --- request-policy reward (Cancel) + the borrower required signer ---------------
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.request_policy_script_ref),
        Redeemer(withdraw_rdmr),
    )
    tx_builder.withdrawals = Withdrawals({reward_address(snapshot.request_policy): 0})
    tx_builder.required_signers = [VerificationKeyHash(snapshot.borrower_pkh)]

    cfg_idx = ref_index(tx_builder)[config_ref]
    mint_rdmr.config_ref_input_index = cfg_idx
    withdraw_rdmr.config_ref_input_index = cfg_idx
