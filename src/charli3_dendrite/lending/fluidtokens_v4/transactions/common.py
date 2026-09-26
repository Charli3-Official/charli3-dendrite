"""Transaction plumbing shared by the FluidTokens V4 builders."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import PlutusV3Script
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import Withdrawals
from pycardano import min_lovelace
from pycardano import plutus_script_hash
from pycardano import script_hash as pycardano_script_hash

from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.units import constr

if TYPE_CHECKING:
    from charli3_dendrite.lending.fluidtokens_v4.datums import CollateralAsset

# Default validity window of a repay or recast. They owe their amount as of the
# window's upper bound, so a short window keeps it close to submission time.
LOAN_ACTION_VALIDITY_SLOTS = 600

# ``Option`` constructor alternative of ``None``: a collateral naming only a policy.
_NONE = 1


def out_ref_of(utxo: Utxo) -> tuple[str, int]:
    """A resolved UTxO's out-ref; raises if it has none."""
    if utxo.out_ref is None:
        raise ValueError("UTxO is missing its out-ref")
    return utxo.out_ref


def min_ada(output: TransactionOutput) -> int:
    """The protocol min-ADA of ``output`` as built, its own coin's encoding included.

    The ledger requires it of every output, while balancing checks only the change
    output and Ogmios evaluation none, so the builders check their own outputs.
    """
    return min_lovelace(EvalContext(last_block_slot=0), output=output)


def is_policy_wide(collateral: CollateralAsset) -> bool:
    """True if ``collateral`` names only a policy (any token under it)."""
    return constr(collateral.maybe_asset_name)[0] == _NONE


def ledger_order(utxos: Iterable[Utxo]) -> list[Utxo]:
    """``utxos`` in the ledger's input order: by transaction id bytes, then index."""
    return sorted(
        utxos,
        key=lambda u: (bytes.fromhex(out_ref_of(u)[0]), out_ref_of(u)[1]),
    )


def script_hash_of(script_ref: Utxo) -> str:
    """The script hash (hex) of a resolved reference-script UTxO."""
    if script_ref.ref_script is None:
        raise ValueError("reference UTxO carries no script")
    return plutus_script_hash(
        PlutusV3Script(bytes.fromhex(script_ref.ref_script)),
    ).payload.hex()


def loan_address(borrower_address: str) -> str:
    """The loan spend script address carrying the borrower's stake credential."""
    borrower = Address.decode(borrower_address)
    return Address(
        payment_part=ScriptHash(bytes.fromhex(c.LOAN_SPEND_SKH)),
        staking_part=borrower.staking_part,
        network=borrower.network,
    ).encode()


def add_zero_withdrawals(
    tx_builder: TransactionBuilder,
    script_hashes: list[str],
) -> None:
    """Add a zero withdrawal per script hash, keeping any already on the builder."""
    withdrawals = dict(tx_builder.withdrawals or {})
    for script_hash in script_hashes:
        withdrawals[reward_address(script_hash)] = 0
    tx_builder.withdrawals = Withdrawals(withdrawals)


def withdraw_redeemer_position(tx_builder: TransactionBuilder, script_hash: str) -> int:
    """Position of a withdraw redeemer in the script context's redeemer list.

    The ledger orders redeemers by purpose (spend, mint, certificate, withdraw) and
    then by key, so a withdraw redeemer sits after every spend, mint and certificate
    redeemer, at the rank of its script hash among the withdraw scripts that carry a
    redeemer. Call this once every script input, mint and withdrawal has been added.
    """
    spends = len(tx_builder._inputs_to_redeemers)
    mints = len(
        {
            plutus_hash(script)
            for script, redeemer in tx_builder._minting_script_to_redeemers
            if redeemer is not None
        },
    )
    certificates = sum(
        1
        for _, redeemer in tx_builder._certificate_script_to_redeemers
        if redeemer is not None
    )
    withdraws = sorted(
        {
            plutus_hash(script)
            for script, redeemer in tx_builder._withdrawal_script_to_redeemers
            if redeemer is not None
        },
    )
    return spends + mints + certificates + withdraws.index(script_hash)


def plutus_hash(script: object) -> str:
    """The hash (hex) of a script held by the builder."""
    return pycardano_script_hash(script).payload.hex()  # type: ignore[arg-type]
