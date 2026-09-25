"""FluidTokens V4 mainnet coordinates.

The protocol is parameterized by a config NFT whose UTxO carries a
:class:`~charli3_dendrite.lending.fluidtokens_v4.datums.ConfigDatum` with every
policy id and script hash. The constants below are the live mainnet values:
:func:`default_config` assembles them into that datum as an offline fallback, and
:func:`resolve_config` re-reads the live datum through the backend so discovery
follows a config update.

The lender-manager spend script is not listed in the config datum: pools opt into it
by pointing ``lender_bond_address`` at it, and it ships as a constant here. Its own
action scripts are listed in the lender-manager config datum, held with a separate
config NFT.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cbor2  # type: ignore[import-not-found]
from pycardano import IndefiniteList
from pycardano import RawPlutusData
from pycardano.exception import DeserializeException

from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

CONFIG_NFT_POLICY = "235b32040fe1177c03b1d34febc470440c6eaaa2228a9c1b0e375200"
CONFIG_NFT_NAME = "706172616d6574657273"  # "parameters"
LENDER_MANAGER_CONFIG_NFT_POLICY = (
    "fb6ae2027358b4a0b62710eb95102d87fa13f66ecf55d8943699c492"
)
LENDER_MANAGER_CONFIG_NFT_NAME = "706172616d6574657273"  # "parameters"

# Config datum values, in datum field order.
SMART_TOKENS_SPEND_SKH = "fca77bcce1e5e73c97a0bfa8c90f7cd2faff6fd6ed5b6fec1c04eefa"
ADMIN_SCRIPT_HASH = "d5940dc2ad9e3f43544fb02a28240f215602603a7519588532b72c53"
POOL_POLICY = "20f765d25da3a36644371f7619d97bdccf034f3067921921d9dce0f7"
REQUEST_POLICY = "9b2e325cc15d14fa23101741e6cc17e8ba977a1563738439968356d2"
BORROWER_BOND_POLICY = "eadc69a5d2d1357acc9b9d49ec5390fcdf6e080c7a40139917223dcb"
LENDER_BOND_POLICY = "bcd713bb7858d4b08738bed90ee7068d8f9b38d02e0cae0b45ac7a9b"
LOAN_POLICY = "20256f557cec14f61cc662468f7812a1e6531e628af257a5fc5dd0c5"
REPAYMENT_POLICY = "addd060da203f54e8ffc8a5d363094bc5e63d85734fe9d2869614d79"
POOL_SPEND_SKH = "ecd0c0fc554beb43ade65d45dd471ede4d9cfc9b22bb70de8e113458"
REQUEST_SPEND_SKH = "390d27f97868840864922fb0b5657ae4982b5be8bf03bf9cec81125e"
LOAN_SPEND_SKH = "b5763e3c9cb7f1a2167f74f76cb635d24d138a4cc5cf9d87659c662d"
LOAN_CLAIM_ACTION_SKH = "63b26ff93c96ae391e0f838de5d62053ec562ea82371c3aaab444902"
LOAN_REPAY_ACTION_SKH = "41ac9c9f0bd2acc2db8af9882b772daee90fcf1ad877f0451bd7af28"
LOAN_CHANGE_COLLATERAL_ACTION_SKH = (
    "7aa6795463a8dcb3534246a6c3d6b1f29a72f920ae1dad6d0bacd43d"
)
LOAN_RECAST_ACTION_SKH = "c0af09a3656074f444e1daec043896c235d18aba0567aba7afb55fe8"
ASSET_MANAGER_SPEND_SKH = "b256da41be022ba1a28fe92b32eeea00109310e3f0aea1cb7791d98f"
POOL_CANCEL_ACTION_SKH = "f7ed285d6a9b9772666c7f379ac3e70d777b9d651026385fd26c3e4d"
POOL_BORROW_ACTION_SKH = "d86664db37ebf149386d3b26cc9b2e9c73bc11319052a6b4927c0a8f"
POOL_SELL_LENDER_POSITION_ACTION_SKH = (
    "fc16ccaa435f24833da06681fdc8a71ae460c41517e87d12baf644fa"
)
POOL_COMPOUND_ACTION_SKH = "fef12ea655d7d562bd419cad05723e201b1f292e6b6be5a874f073ba"
POOL_EDIT_ACTION_SKH = "1ec1853344febb4f5de40fceca2316deb68baa3300161afed4ed6ae0"
POOL_MANAGER_SPEND_SKH = "dfb1d21e529af28d82004eee5689e3304bb896c35ce8fe65c85612b0"
POOL_MANAGER_POLICY = "1e0bf58a4ef7f8f7579b58e290ea9f9283239bb4f7932fe8f29df03a"
LOCKED_BORROWER_MANAGER_SPEND_SKH = (
    "072eee302404c8d869bbc61a2c45502d371d96ec7d6aae7b179bfaa3"
)

# Not carried by the config datum (see the module docstring).
LENDER_MANAGER_SPEND_SKH = "6743f4b69446b4e066cfb89daa3d01ef041b6d2646968993be4cec09"

# Plutus ``Credential``: ``ScriptCredential`` is constructor alternative 1 (tag 122).
_SCRIPT_CREDENTIAL_TAG = 122


def default_config() -> ConfigDatum:
    """The mainnet config datum rebuilt from this module's constants.

    The Dutch auction is disabled on mainnet: its script hashes are empty and its
    parameters are zero.
    """
    return ConfigDatum(
        smart_tokens_spend_script_hash=bytes.fromhex(SMART_TOKENS_SPEND_SKH),
        admin_credential=RawPlutusData(
            cbor2.CBORTag(
                _SCRIPT_CREDENTIAL_TAG,
                IndefiniteList([bytes.fromhex(ADMIN_SCRIPT_HASH)]),
            ),
        ),
        pool_policy_id=bytes.fromhex(POOL_POLICY),
        request_policy_id=bytes.fromhex(REQUEST_POLICY),
        borrower_bond_policy_id=bytes.fromhex(BORROWER_BOND_POLICY),
        lender_bond_policy_id=bytes.fromhex(LENDER_BOND_POLICY),
        loan_policy_id=bytes.fromhex(LOAN_POLICY),
        repayment_policy_id=bytes.fromhex(REPAYMENT_POLICY),
        pool_spend_script_hash=bytes.fromhex(POOL_SPEND_SKH),
        request_spend_script_hash=bytes.fromhex(REQUEST_SPEND_SKH),
        loan_spend_script_hash=bytes.fromhex(LOAN_SPEND_SKH),
        loan_claim_action_script_hash=bytes.fromhex(LOAN_CLAIM_ACTION_SKH),
        loan_repay_action_script_hash=bytes.fromhex(LOAN_REPAY_ACTION_SKH),
        loan_change_collateral_action_script_hash=bytes.fromhex(
            LOAN_CHANGE_COLLATERAL_ACTION_SKH,
        ),
        loan_recast_action_script_hash=bytes.fromhex(LOAN_RECAST_ACTION_SKH),
        asset_manager_spend_script_hash=bytes.fromhex(ASSET_MANAGER_SPEND_SKH),
        dutch_auction_spend_script_hash=b"",
        dutch_auction_withdraw_script_hash=b"",
        dutch_auction_starting_increase_per_mille=0,
        dutch_auction_lowering_amount=0,
        dutch_auction_lowering_frequency=0,
        dutch_auction_min_price_to_cancel=0,
        pool_cancel_action_script_hash=bytes.fromhex(POOL_CANCEL_ACTION_SKH),
        pool_borrow_action_script_hash=bytes.fromhex(POOL_BORROW_ACTION_SKH),
        pool_sell_lender_position_action_script_hash=bytes.fromhex(
            POOL_SELL_LENDER_POSITION_ACTION_SKH,
        ),
        pool_compound_action_script_hash=bytes.fromhex(POOL_COMPOUND_ACTION_SKH),
        pool_edit_action_script_hash=bytes.fromhex(POOL_EDIT_ACTION_SKH),
        pool_manager_spend_script_hash=bytes.fromhex(POOL_MANAGER_SPEND_SKH),
        pool_manager_policy_id=bytes.fromhex(POOL_MANAGER_POLICY),
        locked_borrower_manager_spend_script_hash=bytes.fromhex(
            LOCKED_BORROWER_MANAGER_SPEND_SKH,
        ),
    )


def resolve_config(backend: AbstractBackend) -> ConfigDatum:
    """Read the live config datum held with the config NFT.

    Falls back to :func:`default_config` when the datum cannot be read or decoded (a
    backend without dbsync queries, a missing config UTxO or datum, or a datum whose
    layout no longer matches :class:`ConfigDatum`), so discovery keeps working offline.
    Database errors propagate.
    """
    # Imported lazily: the V3 resolver pulls in the V3 transaction context.
    from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
        resolve_utxo_by_asset,
    )

    try:
        utxo = resolve_utxo_by_asset(backend, CONFIG_NFT_POLICY, CONFIG_NFT_NAME)
    except (TypeError, ValueError):
        return default_config()
    if not utxo.datum:
        return default_config()
    try:
        return ConfigDatum.from_cbor(utxo.datum)
    except (DeserializeException, ValueError, cbor2.CBORDecodeError):
        return default_config()
