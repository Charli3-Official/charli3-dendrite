"""Backend-free tests for the Dano batch swap redeemer serialization.

A Dano swap tx spends N >= 1 pool UTxOs under one withdraw-zero; every Spend +
the single Withdraw redeemer carry the same batch vector of
``(pool_in_idx, pool_out_idx, delta)`` entries. A payload longer than 64 bytes
(any batch of >= 2 pools) MUST serialize as a CBOR indefinite-length chunked
byte string, or the ledger rejects the transaction as malformed.
"""

from __future__ import annotations

import cbor2
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano.serialization import ByteString
from pycardano.serialization import default_encoder

from charli3_dendrite.dexs.amm.dano import DanoSwapRedeemer
from charli3_dendrite.dexs.amm.dano import build_batch_swap_redeemer_bytes
from charli3_dendrite.dexs.amm.dano import parse_swap_redeemer_bytes


def _redeemer(is_withdraw: bool = False) -> DanoSwapRedeemer:
    return DanoSwapRedeemer(
        pool_input=TransactionInput(TransactionId(bytes(32)), 0),
        delta_amount=100,
        is_withdraw=is_withdraw,
    )


def test_to_primitive_wraps_the_full_batch_in_a_bytestring() -> None:
    """``to_primitive`` returns the packed batch vector wrapped in ``ByteString``."""
    entries = [(3, 0, 100), (0, 1, -200)]
    r = _redeemer()
    r.first_byte = 3
    r._entries = entries

    prim = r.to_primitive()

    assert isinstance(prim, ByteString)
    assert prim.value == build_batch_swap_redeemer_bytes(first_byte=3, entries=entries)
    # 2 pools -> 2 header bytes + 34 per entry = 70-byte payload (> 64).
    assert len(prim.value) == 70


def test_two_pool_payload_serializes_as_chunked_indefinite_bytestring() -> None:
    """A > 64-byte payload must CBOR-encode as an indefinite (chunked) byte string."""
    entries = [(3, 0, 100), (0, 1, -200)]
    r = _redeemer()
    r.first_byte = 3
    r._entries = entries

    encoded = cbor2.dumps(r.to_primitive(), default=default_encoder)

    assert encoded[0] == 0x5F  # 0x5f == indefinite-length byte string (definite: 0x58)
    first_byte, _action, decoded = parse_swap_redeemer_bytes(cbor2.loads(encoded))
    assert first_byte == 3
    assert decoded == entries


def test_single_pool_payload_stays_a_definite_bytestring() -> None:
    """A <= 64-byte (single-pool) payload stays definite, byte-identical to before."""
    r = _redeemer()
    r.first_byte = 0
    r._entries = [(0, 0, 100)]

    encoded = cbor2.dumps(r.to_primitive(), default=default_encoder)

    assert encoded[0] == 0x58  # definite-length byte string
    first_byte, _action, decoded = parse_swap_redeemer_bytes(cbor2.loads(encoded))
    assert first_byte == 0
    assert decoded == [(0, 0, 100)]
