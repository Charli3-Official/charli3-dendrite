"""Transaction plumbing shared by the FluidTokens V4 builders.

``out_ref_of``, ``min_ada``, ``settled_min_ada`` and ``script_hash_of`` live in the
shared ``lending.transactions`` seam and are re-exported here for existing callers.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import Withdrawals
from pycardano import script_hash as pycardano_script_hash

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.transactions.common import min_ada  # noqa: F401
from charli3_dendrite.lending.transactions.common import out_ref_of
from charli3_dendrite.lending.transactions.common import reward_address
from charli3_dendrite.lending.transactions.common import settled_min_ada  # noqa: F401
from charli3_dendrite.lending.transactions.utxos import script_hash_of  # noqa: F401
from charli3_dendrite.lending.units import constr

if TYPE_CHECKING:
    from pycardano import Datum

    from charli3_dendrite.lending.fluidtokens_v4.datums import CollateralAsset

# Default validity window of a repay or recast. They owe their amount as of the
# window's upper bound, so a short window keeps it close to submission time.
LOAN_ACTION_VALIDITY_SLOTS = 600

# ``Option`` constructor alternative of ``None``: a collateral naming only a policy.
_NONE = 1


def signing_key(auth: Datum, what: str) -> bytes:
    """The key hash of an ``AuthorizationMethod`` that is a signature.

    Raises ``NotImplementedError`` for any other authorisation: this library signs
    with wallet keys only. ``what`` names the owned thing in the message.
    """
    alt, fields = constr(auth)
    if alt != AuthCardanoSignature.CONSTR_ID:
        raise NotImplementedError(f"only a {what} owned by a key is supported")
    return bytes(fields[0])


def sole_nft_name(utxo: Utxo, policy: str, what: str) -> bytes:
    """The name of the one NFT of ``policy`` a ``what`` UTxO holds."""
    names = [n for p, n, q in utxo.assets if p == policy and q == 1]
    if len(names) != 1:
        raise ValueError(f"{what} UTxO must hold exactly one {what} NFT")
    return bytes.fromhex(names[0])


def pool_nft_name(pool: Utxo) -> bytes:
    """The name of the one pool NFT a pool UTxO holds."""
    return sole_nft_name(pool, c.POOL_POLICY, "pool")


def is_policy_wide(collateral: CollateralAsset) -> bool:
    """True if ``collateral`` names only a policy (any token under it)."""
    return constr(collateral.maybe_asset_name)[0] == _NONE


def ledger_order(utxos: Iterable[Utxo]) -> list[Utxo]:
    """``utxos`` in the ledger's input order: by transaction id bytes, then index."""
    return sorted(
        utxos,
        key=lambda u: (bytes.fromhex(out_ref_of(u)[0]), out_ref_of(u)[1]),
    )


def captured_config(refs: Iterable[Utxo]) -> tuple[Utxo, ConfigDatum]:
    """The config UTxO among a captured transaction's reference inputs, and its datum.

    The config moves when FluidTokens upgrades an action script, so a capture names
    the scripts it ran through the config it referenced, not through the constants.
    """
    config = next(
        (u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)),
        None,
    )
    if config is None:
        raise ValueError("the transaction has no config reference input")
    if config.datum is None:
        raise ValueError("config UTxO is missing its datum")
    return config, ConfigDatum.from_cbor(config.datum)


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
