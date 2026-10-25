"""Low-level transaction helpers shared by the lending transaction builders.

Canonical-ordering index lookups (the Plutus script context sorts inputs and
reference inputs by ``(tx_id, index)``), the reward (stake-script) address encoding,
integer redeemers, the ledger minimum-ADA of an output, and zero withdrawals and
reference inputs merged into a builder that several legs contribute to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Network
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Withdrawals
from pycardano import min_lovelace

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable

    from pycardano import Datum
    from pycardano import PlutusData

    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "add_zero_withdrawal",
    "input_index",
    "merge_reference_inputs",
    "min_ada",
    "out_ref_of",
    "plutus_int",
    "ref_index",
    "reward_address",
    "settled_min_ada",
    "with_reference_script",
]


def ref_index(tx_builder: TransactionBuilder) -> dict[tuple[str, int], int]:
    """Map each reference input's out-ref to its index in the canonical ordering."""
    inputs = [
        u.input if isinstance(u, UTxO) else u for u in tx_builder.reference_inputs
    ]
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


def plutus_int(n: int) -> RawPlutusData:
    """An integer Plutus datum or redeemer: the bare CBOR integer ``n``.

    `assemble_unsigned` serializes it as given, so ``plutus_int(0)`` encodes ``00``.
    """
    return RawPlutusData(n)


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


def _reference_member(
    tx_builder: TransactionBuilder,
    tx_input: TransactionInput,
) -> UTxO | TransactionInput | None:
    """The builder's reference input at ``tx_input`` (a `UTxO` or bare input)."""
    for member in tx_builder.reference_inputs:
        member_input = member.input if isinstance(member, UTxO) else member
        if member_input == tx_input:
            return member
    return None


def add_zero_withdrawal(
    tx_builder: TransactionBuilder,
    *,
    script_ref: Utxo,
    redeemer: PlutusData | RawPlutusData,
) -> None:
    """Withdraw zero from the reward account of the script ``script_ref`` carries.

    The script runs from ``script_ref`` as a reference input, typed by its recorded
    language, with ``redeemer``. The withdrawal merges into those already on the
    builder; when the account is already among them, nothing is added. When the
    builder already references ``script_ref``'s out-ref, that reference input is
    kept and no second one is added.
    """
    account = reward_address(script_hash_of(script_ref))
    withdrawals = dict(tx_builder.withdrawals or {})
    if account in withdrawals:
        return
    utxo = to_pycardano_utxo(script_ref)
    withdrawals[account] = 0
    tx_builder.withdrawals = Withdrawals(withdrawals)
    _add_by_reference(
        tx_builder,
        utxo,
        lambda ref: tx_builder.add_withdrawal_script(ref, Redeemer(redeemer)),
    )


def with_reference_script(
    tx_builder: TransactionBuilder,
    script_ref: Utxo,
    add: Callable[[UTxO], object],
) -> None:
    """Call ``add`` with ``script_ref`` as a pycardano UTxO, once per out-ref.

    ``add`` is a pycardano call that runs the script by reference
    (``add_script_input``, ``add_minting_script``, ``add_withdrawal_script``).
    When the builder already references ``script_ref``'s out-ref, the reference
    input already held stays, and the script is not counted a second time in the
    reference-script size the fee is computed from.
    """
    _add_by_reference(tx_builder, to_pycardano_utxo(script_ref), add)


def _add_by_reference(
    tx_builder: TransactionBuilder,
    utxo: UTxO,
    add: Callable[[UTxO], object],
) -> None:
    existing = _reference_member(tx_builder, utxo.input)
    counted = len(tx_builder._reference_scripts)
    add(utxo)
    if existing is None:
        return
    if existing != utxo:
        tx_builder.reference_inputs.discard(utxo)
    script = utxo.output.script
    if script is not None and script in tx_builder._reference_scripts[:counted]:
        del tx_builder._reference_scripts[counted:]


def merge_reference_inputs(
    tx_builder: TransactionBuilder,
    utxos: Iterable[Utxo],
) -> None:
    """Add each of ``utxos`` as a reference input unless its out-ref is one already.

    An out-ref counts once whether the builder holds it as a `UTxO` or as a bare
    `TransactionInput`; of several UTxOs at one out-ref, the first one added stays.
    """
    for u in utxos:
        utxo = to_pycardano_utxo(u)
        if _reference_member(tx_builder, utxo.input) is None:
            tx_builder.reference_inputs.add(utxo)
