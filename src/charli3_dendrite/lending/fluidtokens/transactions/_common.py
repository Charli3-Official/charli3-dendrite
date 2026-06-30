"""Shared low-level helpers for the FluidTokens loan-action transaction builders.

Canonical-ordering index lookups (the Plutus script context sorts inputs / reference
inputs by ``(tx_id, index)``) + the reward (stake-script) address encoding + the
validity-window cap shared by every loan action.
"""

from __future__ import annotations

from dataclasses import dataclass

from pycardano import Address
from pycardano import Datum
from pycardano import Network
from pycardano import PlutusData
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import UTxO
from pycardano import VerificationKeyHash

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


def value_map_indices(
    lovelace: int,
    assets: list[tuple[str, str, int]],
    policy_hex: str,
    name_hex: str,
) -> tuple[int, int]:
    """Position of (policy, asset name) inside a UTxO's on-chain value map.

    The loan validators' ``efficient_quantity_of`` reads the reference input's value as
    a raw CBOR map and indexes it positionally: ``policyIdIndex`` into the policy
    entries and ``assetNameIndex`` into that policy's asset entries. The Cardano ledger
    serializes ``Value`` as an ascending-key map (lovelace under the empty policy id
    sorts first), so we reproduce that ordering with plain ``bytes`` comparison.
    """
    policies: dict[bytes, set[bytes]] = {}
    if lovelace:
        policies.setdefault(b"", set()).add(b"")
    for policy, name, _qty in assets:
        policies.setdefault(bytes.fromhex(policy), set()).add(bytes.fromhex(name))
    sorted_policies = sorted(policies)
    target_policy = bytes.fromhex(policy_hex)
    policy_id_index = sorted_policies.index(target_policy)
    asset_name_index = sorted(policies[target_policy]).index(bytes.fromhex(name_hex))
    return policy_id_index, asset_name_index


@dataclass
class _VKeyCredential(PlutusData):
    """``Credential.VerificationKey`` == Constr0([key_hash])."""

    CONSTR_ID = 0
    cred_hash: bytes


@dataclass
class _ScriptCredential(PlutusData):
    """``Credential.Script`` == Constr1([script_hash])."""

    CONSTR_ID = 1
    cred_hash: bytes


@dataclass
class _Inline(PlutusData):
    """``Referenced.Inline`` == Constr0([credential])."""

    CONSTR_ID = 0
    credential: Datum


@dataclass
class _StakeSome(PlutusData):
    """``Option.Some`` (stake reference present) == Constr0([referenced])."""

    CONSTR_ID = 0
    referenced: Datum


@dataclass
class _StakeNone(PlutusData):
    """``Option.None`` (no stake reference) == Constr1([])."""

    CONSTR_ID = 1


@dataclass
class _PlutusAddress(PlutusData):
    """Plutus ``Address`` == Constr0([payment_credential, Option<stake>])."""

    CONSTR_ID = 0
    payment_credential: Datum
    stake_credential: Datum


def plutus_address(address: Address) -> PlutusData:
    """The Plutus ``cardano/address.Address`` PlutusData for a pycardano `Address`.

    Encodes the payment credential (verification-key -> Constr0 / script -> Constr1) and
    the stake credential as ``Some(Inline(credential))`` (base address) or ``None``
    (enterprise address); stake pointers are not produced (FluidTokens addresses use the
    inline form). Reproduces the on-chain borrower-address bytes byte-exact.
    """
    pay = address.payment_part
    payment = (
        _VKeyCredential(bytes(pay))
        if isinstance(pay, VerificationKeyHash)
        else _ScriptCredential(bytes(pay))
    )
    stake = address.staking_part
    if stake is None:
        stake_credential: PlutusData = _StakeNone()
    else:
        inner = (
            _VKeyCredential(bytes(stake))
            if isinstance(stake, VerificationKeyHash)
            else _ScriptCredential(bytes(stake))
        )
        stake_credential = _StakeSome(_Inline(inner))
    return _PlutusAddress(payment, stake_credential)


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
