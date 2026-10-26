"""Tests for ``ScriptReference.to_utxo``."""

from __future__ import annotations

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import PlutusV2Script
from pycardano import PlutusV3Script
from pycardano import plutus_script_hash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dexs.ob.cardanoswaps import SWAP_VALIDATOR_HASH
from charli3_dendrite.dexs.ob.cardanoswaps import SWAP_VALIDATOR_SCRIPT_HEX

REF_TX = "ab" * 32
REF_ADDRESS = str(
    Address(
        payment_part=plutus_script_hash(PlutusV2Script(b"\x01")),
        network=Network.MAINNET,
    ),
)


def _reference(script: str | None, tx_hash: str | None = REF_TX) -> ScriptReference:
    return ScriptReference(
        tx_hash=tx_hash,
        tx_index=0 if tx_hash is not None else None,
        address=REF_ADDRESS if tx_hash is not None else None,
        assets=Assets(lovelace=20_000_000) if tx_hash is not None else None,
        datum_hash=None,
        datum_cbor=None,
        script=script,
    )


def test_default_to_utxo_is_unchanged():
    utxo = _reference("4e4d01000033222220051200120011").to_utxo()

    assert utxo is not None
    assert isinstance(utxo.output.script, PlutusV2Script)
    assert utxo.output.amount.coin == 20_000_000
    assert not utxo.output.amount.multi_asset
    assert str(utxo.input.transaction_id) == REF_TX
    assert utxo.input.index == 0


def test_to_utxo_types_the_script_by_version():
    utxo = _reference(SWAP_VALIDATOR_SCRIPT_HEX).to_utxo(
        PlutusV3Script,
        expected_hash=SWAP_VALIDATOR_HASH,
    )

    assert utxo is not None
    assert isinstance(utxo.output.script, PlutusV3Script)
    assert plutus_script_hash(utxo.output.script).payload.hex() == SWAP_VALIDATOR_HASH


def test_to_utxo_rejects_a_hash_mismatch():
    with pytest.raises(ValueError, match=SWAP_VALIDATOR_HASH):
        _reference(SWAP_VALIDATOR_SCRIPT_HEX).to_utxo(
            PlutusV3Script,
            expected_hash="00" * 28,
        )


def test_to_utxo_without_tx_hash_is_none():
    assert _reference(None, tx_hash=None).to_utxo() is None
    assert (
        _reference(None, tx_hash=None).to_utxo(
            PlutusV3Script,
            expected_hash=SWAP_VALIDATOR_HASH,
        )
        is None
    )
