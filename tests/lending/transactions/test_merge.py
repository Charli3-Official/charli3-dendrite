"""The merge helpers: validity windows, zero withdrawals and reference inputs.

Each helper merges into whatever a builder already holds, so several legs can
contribute to one transaction without overwriting each other.
"""

from __future__ import annotations

import cbor2
import pytest
from pycardano import Address
from pycardano import Network
from pycardano import PlutusV2Script
from pycardano import Transaction
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import VerificationKeyHash
from pycardano import Withdrawals

from charli3_dendrite.lending.transactions.common import add_zero_withdrawal
from charli3_dendrite.lending.transactions.common import plutus_int
from charli3_dendrite.lending.transactions.common import reward_address
from charli3_dendrite.lending.transactions.errors import LendingBuildError
from charli3_dendrite.lending.transactions.errors import ValidityWindowError
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.transactions.validity import merge_validity


# Two distinct PlutusV2 script bodies; only their bytes and hashes matter here.
SCRIPT_A = "4e4d01000033222220051200120011"
SCRIPT_B = "4e4d01000033222220051200120012"

WALLET = Address(VerificationKeyHash(bytes(28)), network=Network.MAINNET)
SCRIPT_HOLDER = Address(VerificationKeyHash(bytes(range(28))), network=Network.MAINNET)


def _builder() -> TransactionBuilder:
    return TransactionBuilder(EvalContext(last_block_slot=0))


def _script_ref(script: str, tx_byte: int, index: int = 0, datum: str | None = None):
    return Utxo(
        address=SCRIPT_HOLDER.encode(),
        lovelace=20_000_000,
        assets=[],
        datum=datum,
        ref_script=script,
        ref_script_type="plutusV2",
        out_ref=(f"{tx_byte:02x}" * 32, index),
    )


def _with_wallet_input(tx_builder: TransactionBuilder) -> TransactionBuilder:
    tx_builder.add_input(
        UTxO(
            TransactionInput(TransactionId(bytes(32)), 0),
            TransactionOutput(WALLET, 5_000_000),
        ),
    )
    return tx_builder


def _reference_out_refs(tx_builder: TransactionBuilder) -> list[tuple[str, int]]:
    out_refs = []
    for member in tx_builder.reference_inputs:
        tx_input = member.input if isinstance(member, UTxO) else member
        out_refs.append((bytes(tx_input.transaction_id).hex(), tx_input.index))
    return sorted(out_refs)


def test_validity_intersects_with_an_existing_window():
    tx_builder = _builder()
    tx_builder.ttl = 100

    merge_validity(tx_builder, upper_slot=90, lower_slot=10)
    assert (tx_builder.validity_start, tx_builder.ttl) == (10, 90)

    # A later, looser bound never widens the window.
    merge_validity(tx_builder, upper_slot=95, lower_slot=5)
    assert (tx_builder.validity_start, tx_builder.ttl) == (10, 90)

    # A tighter one narrows it.
    merge_validity(tx_builder, upper_slot=80, lower_slot=20)
    assert (tx_builder.validity_start, tx_builder.ttl) == (20, 80)


def test_validity_on_an_empty_builder_sets_the_bounds_given():
    tx_builder = _builder()
    merge_validity(tx_builder, upper_slot=90)
    assert (tx_builder.validity_start, tx_builder.ttl) == (None, 90)

    merge_validity(tx_builder, upper_slot=90, lower_slot=None)
    assert tx_builder.validity_start is None


def test_validity_lower_bound_at_or_past_the_upper_raises():
    tx_builder = _builder()
    tx_builder.ttl = 90
    with pytest.raises(ValidityWindowError):
        merge_validity(tx_builder, upper_slot=100, lower_slot=95)
    # The ttl is exclusive (invalid-hereafter): a start equal to it is empty too.
    with pytest.raises(ValidityWindowError):
        merge_validity(tx_builder, upper_slot=100, lower_slot=90)
    # A refused merge leaves the builder as it was.
    assert (tx_builder.validity_start, tx_builder.ttl) == (None, 90)


def test_validity_upper_bound_below_an_existing_start_raises():
    tx_builder = _builder()
    tx_builder.validity_start = 50
    with pytest.raises(ValidityWindowError) as info:
        merge_validity(tx_builder, upper_slot=40)
    assert isinstance(info.value, LendingBuildError)
    assert isinstance(info.value, ValueError)
    assert (tx_builder.validity_start, tx_builder.ttl) == (50, None)


def test_zero_withdrawal_by_reference_script():
    ref = _script_ref(SCRIPT_A, 0xA1)
    tx_builder = _builder()
    add_zero_withdrawal(tx_builder, script_ref=ref, redeemer=plutus_int(0))

    assert dict(tx_builder.withdrawals) == {reward_address(script_hash_of(ref)): 0}
    [(script, redeemer)] = tx_builder._withdrawal_script_to_redeemers
    # The script keeps its recorded language, which the redeemer indexing needs.
    assert isinstance(script, PlutusV2Script)
    assert redeemer.data.to_cbor_hex() == "00"
    assert _reference_out_refs(tx_builder) == [ref.out_ref]


def test_zero_withdrawal_twice_adds_it_once():
    ref = _script_ref(SCRIPT_A, 0xA1)
    tx_builder = _builder()
    add_zero_withdrawal(tx_builder, script_ref=ref, redeemer=plutus_int(0))
    add_zero_withdrawal(tx_builder, script_ref=ref, redeemer=plutus_int(0))

    assert len(tx_builder.withdrawals) == 1
    assert len(tx_builder._withdrawal_script_to_redeemers) == 1
    assert len(tx_builder.reference_inputs) == 1


def test_zero_withdrawal_merges_with_an_existing_withdrawal():
    other = "ab" * 28
    ref = _script_ref(SCRIPT_A, 0xA1)
    tx_builder = _builder()
    tx_builder.withdrawals = Withdrawals({reward_address(other): 0})

    add_zero_withdrawal(tx_builder, script_ref=ref, redeemer=plutus_int(1))

    assert dict(tx_builder.withdrawals) == {
        reward_address(other): 0,
        reward_address(script_hash_of(ref)): 0,
    }


def test_zero_withdrawal_already_on_the_builder_is_left_alone():
    # A withdrawal some other leg added for the same script is not added again.
    ref = _script_ref(SCRIPT_A, 0xA1)
    tx_builder = _builder()
    tx_builder.withdrawals = Withdrawals({reward_address(script_hash_of(ref)): 0})

    add_zero_withdrawal(tx_builder, script_ref=ref, redeemer=plutus_int(0))

    assert len(tx_builder.withdrawals) == 1
    assert tx_builder._withdrawal_script_to_redeemers == []
    assert tx_builder.reference_inputs == set()


def test_zero_withdrawal_keeps_a_reference_input_already_present():
    # The script's out-ref is already referenced through a differently decoded
    # UTxO: the builder keeps that one member, so reference indices stay unique.
    ref = _script_ref(SCRIPT_A, 0xA1)
    first = _script_ref(SCRIPT_A, 0xA1, datum="d87980")
    tx_builder = _builder()
    first_utxo = to_pycardano_utxo(first)
    tx_builder.reference_inputs.add(first_utxo)

    add_zero_withdrawal(tx_builder, script_ref=ref, redeemer=plutus_int(0))

    assert tx_builder.reference_inputs == {first_utxo}
    assert len(tx_builder._withdrawal_script_to_redeemers) == 1


def test_two_zero_withdrawals_assemble_with_their_redeemers():
    ref_a = _script_ref(SCRIPT_A, 0xA1)
    ref_b = _script_ref(SCRIPT_B, 0xB2, 3)
    tx_builder = _with_wallet_input(_builder())
    add_zero_withdrawal(tx_builder, script_ref=ref_a, redeemer=plutus_int(0))
    add_zero_withdrawal(tx_builder, script_ref=ref_b, redeemer=plutus_int(1))

    tx = Transaction.from_cbor(assemble_unsigned(tx_builder))

    accounts = sorted(
        [reward_address(script_hash_of(ref_a)), reward_address(script_hash_of(ref_b))],
    )
    assert list(tx.transaction_body.withdraws) == accounts
    redeemers = {
        key.index: cbor2.dumps(value.data).hex()
        for key, value in tx.transaction_witness_set.redeemer.items()
    }
    # Each withdraw redeemer sits at its account's position in the sorted map.
    assert redeemers == {
        accounts.index(reward_address(script_hash_of(ref_a))): "00",
        accounts.index(reward_address(script_hash_of(ref_b))): "01",
    }
    assert {key.tag.name for key in tx.transaction_witness_set.redeemer} == {
        "WITHDRAWAL",
    }
