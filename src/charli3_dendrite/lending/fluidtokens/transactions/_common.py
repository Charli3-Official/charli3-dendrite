"""Shared low-level helpers for the FluidTokens loan-action transaction builders.

Canonical-ordering index lookups (the Plutus script context sorts inputs / reference
inputs by ``(tx_id, index)``) + the reward (stake-script) address encoding + the
validity-window cap shared by every loan action.
"""
from __future__ import annotations

from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import UTxO

# The FluidTokens loan-action validators cap the validity window; 360 slots mirrors the
# Danogo cap (the upper bound the validator's time checks tolerate).
LOAN_ACTION_VALIDITY_SLOTS = 360


def ref_index(tx_builder: TransactionBuilder) -> dict[tuple[str, int], int]:
    """Map each reference input's out-ref to its index in the canonical ordering."""
    inputs = [u.input for u in tx_builder.reference_inputs if isinstance(u, UTxO)]
    inputs.sort(key=lambda i: (bytes(i.transaction_id), i.index))
    return {
        (bytes(i.transaction_id).hex(), i.index): pos for pos, i in enumerate(inputs)
    }


def input_index(tx_builder: TransactionBuilder, out_ref: tuple[str, int]) -> int:
    """Index of `out_ref` in the canonical (tx id, index) input ordering."""
    inputs = sorted(
        tx_builder.inputs,
        key=lambda u: (bytes(u.input.transaction_id), u.input.index),
    )
    for pos, u in enumerate(inputs):
        if (bytes(u.input.transaction_id).hex(), u.input.index) == out_ref:
            return pos
    raise ValueError(f"input {out_ref} not found")


def reward_address(script_hash: str) -> bytes:
    """The network-tagged reward (stake-script) address bytes for `script_hash`."""
    return bytes(
        Address(
            staking_part=ScriptHash(bytes.fromhex(script_hash)),
            network=Network.MAINNET,
        ),
    )


def set_validity_window(tx_builder: TransactionBuilder) -> None:
    """Pin the builder's validity window to the loan-action cap from its lower bound."""
    lower = (
        tx_builder.validity_start
        if tx_builder.validity_start is not None
        else tx_builder.context.last_block_slot
    )
    tx_builder.validity_start = lower
    tx_builder.ttl = lower + LOAN_ACTION_VALIDITY_SLOTS
