"""Low-level transaction helpers shared by the lending transaction builders.

Canonical-ordering index lookups (the Plutus script context sorts inputs and
reference inputs by ``(tx_id, index)``), the reward (stake-script) address encoding,
and the ledger minimum-ADA of an output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import min_lovelace

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from pycardano import Datum

    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "input_index",
    "min_ada",
    "out_ref_of",
    "ref_index",
    "reward_address",
    "settled_min_ada",
]


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


def settled_min_ada(address: Address, assets: dict[str, int], datum: Datum) -> int:
    """The least ADA an output of ``assets`` and ``datum`` at ``address`` can hold.

    That is the least lovelace amount that covers the min-ADA of the output holding
    it: the coin's own encoding grows with the amount, so the minimum is iterated to
    its fixed point. ``assets`` maps unit to quantity and excludes lovelace.
    """
    lovelace = 0
    while True:
        output = TransactionOutput(
            address,
            asset_to_value(Assets(**{"lovelace": lovelace, **assets})),
            datum=datum,
        )
        needed = min_ada(output)
        if needed <= lovelace:
            return lovelace
        lovelace = needed
