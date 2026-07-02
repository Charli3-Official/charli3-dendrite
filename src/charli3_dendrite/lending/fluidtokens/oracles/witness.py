"""Parser for the FluidTokens signed oracle-reward redeemer.

BORROW and MODIFY_COLLATERAL transactions re-price the loan collateral through an
"oracle reward" ``Withdraw`` script whose redeemer is a time-bound, signed price
message produced off-chain by the protocol's oracle signer. The redeemer is a
Plutus ``Constr`` (decoded here with ``cbor2``) shaped as::

    Constr(0, [
        Constr(0, [
            Constr(0, [ valid_from_ms, valid_to_ms, Constr(0, [policy, name]) ]),
            price_num,
            price_den,
        ]),
        [ Constr(0, [ signature, index ]), ... ],
    ])

:class:`OracleReward` decodes that message into typed fields and keeps the raw
cbor so a builder can replay the witness verbatim, and derives the transaction
validity window the signed message covers.
"""

from __future__ import annotations

from dataclasses import dataclass

import cbor2  # type: ignore[import-not-found]

from charli3_dendrite.utility import posix_ms_to_slot
from charli3_dendrite.utility import slot_to_posix_ms

# Plutus ``Constr`` alternative 0 encodes to CBOR tag 121.
_CONSTR_0 = 121
# Ed25519 signatures are always 64 bytes.
_ED25519_SIG_LEN = 64


def _constr_fields(value: object, arity: int, what: str) -> list:
    """Return the fields of a ``Constr`` alternative-0 tag, validating shape."""
    if not isinstance(value, cbor2.CBORTag) or value.tag != _CONSTR_0:
        raise ValueError(f"oracle reward {what}: expected Constr(0, ...)")
    fields = value.value
    if not isinstance(fields, list) or len(fields) != arity:
        raise ValueError(f"oracle reward {what}: expected {arity} fields")
    return fields


def _constr0(fields: list) -> cbor2.CBORTag:
    """Wrap ``fields`` in a Plutus ``Constr`` alternative-0 (CBOR tag 121)."""
    return cbor2.CBORTag(_CONSTR_0, fields)


def build_oracle_reward_cbor(
    *,
    valid_from_ms: int,
    valid_to_ms: int,
    collateral_policy: str,
    collateral_name: str,
    price_num: int,
    price_den: int,
    signatures: list[tuple[bytes, int]],
) -> str:
    """Assemble a signed oracle-reward redeemer cbor-hex from its parts.

    Inverse of :meth:`OracleReward.parse`: builds the ``Constr`` message (validity
    window + priced token + rational price) and the list of
    ``(ed25519_signature, signer_index)`` entries into the redeemer the oracle
    ``Withdraw`` script expects. ``collateral_policy`` / ``collateral_name`` are hex;
    each ``signer_index`` selects the signing key within the oracle's ordered public
    keys.

    The output is deliberately NOT canonicalized: the transaction builder round-trips
    the redeemer through ``pycardano.RawPlutusData`` and the ledger re-encodes the
    message that the on-chain ``serialise_data`` signature check verifies, so only the
    structure and values -- never the raw byte layout -- must be correct. A redeemer
    built here therefore serializes on-chain identically to a captured one carrying the
    same message and signatures.

    Raises :class:`ValueError` on an empty signature list or a non-64-byte signature.
    """
    if not signatures:
        raise ValueError("oracle reward: at least one signature is required")
    signature_entries: list[cbor2.CBORTag] = []
    for signature, index in signatures:
        if not isinstance(signature, (bytes, bytearray)):
            raise ValueError("oracle reward: signature must be bytes")
        if len(signature) != _ED25519_SIG_LEN:
            raise ValueError(
                f"oracle reward: signature must be {_ED25519_SIG_LEN} bytes",
            )
        signature_entries.append(_constr0([bytes(signature), index]))

    token = _constr0([bytes.fromhex(collateral_policy), bytes.fromhex(collateral_name)])
    message = _constr0([valid_from_ms, valid_to_ms, token])
    signed = _constr0([message, price_num, price_den])
    redeemer = _constr0([signed, signature_entries])
    return cbor2.dumps(redeemer).hex()


@dataclass(frozen=True)
class OracleReward:
    """A decoded, signed oracle-reward price message.

    `valid_from_ms` / `valid_to_ms` bound the message in POSIX milliseconds;
    `collateral_policy` / `collateral_name` (hex) identify the priced token; the
    price is the rational `price_num / price_den`. `signature` is the first
    signer's 64-byte Ed25519 signature over the message, and `cbor` is the raw
    redeemer hex so a builder can replay the witness verbatim.
    """

    valid_from_ms: int
    valid_to_ms: int
    collateral_policy: str
    collateral_name: str
    price_num: int
    price_den: int
    signature: bytes
    cbor: str

    @classmethod
    def parse(cls, cbor_hex: str) -> OracleReward:
        """Decode an oracle-reward redeemer cbor-hex into an :class:`OracleReward`.

        Robust to the list-of-signatures wrapper (the first signature is taken).
        Raises :class:`ValueError` if the cbor does not match the expected shape.
        """
        try:
            top = cbor2.loads(bytes.fromhex(cbor_hex))
        except (ValueError, cbor2.CBORDecodeError) as exc:
            raise ValueError(f"oracle reward: undecodable cbor: {exc}") from exc

        signed, signatures = _constr_fields(top, 2, "envelope")
        message, price_num, price_den = _constr_fields(signed, 3, "message")
        valid_from_ms, valid_to_ms, token = _constr_fields(message, 3, "window")
        policy, name = _constr_fields(token, 2, "token")

        if not (
            isinstance(valid_from_ms, int)
            and isinstance(valid_to_ms, int)
            and isinstance(price_num, int)
            and isinstance(price_den, int)
        ):
            raise ValueError("oracle reward: window/price fields must be integers")
        if not (isinstance(policy, bytes) and isinstance(name, bytes)):
            raise ValueError("oracle reward: token policy/name must be bytes")

        if not isinstance(signatures, list) or not signatures:
            raise ValueError("oracle reward: expected a non-empty signature list")
        signature, _index = _constr_fields(signatures[0], 2, "signature")
        if not isinstance(signature, bytes):
            raise ValueError("oracle reward: signature must be bytes")

        return cls(
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            collateral_policy=policy.hex(),
            collateral_name=name.hex(),
            price_num=price_num,
            price_den=price_den,
            signature=signature,
            cbor=cbor_hex,
        )

    def tx_validity_slots(self) -> tuple[int, int]:
        """The widest ``(from, to)`` slot window the signed ms window fully covers.

        A slot's wall-clock time is its start instant, so the covered start slot is
        the first slot beginning at or after `valid_from_ms` (ceil) and the covered
        end slot is the last slot beginning at or before `valid_to_ms` (floor). By
        construction :meth:`contains_slot_window` accepts the returned window; a
        builder can pin the transaction's validity range to (a subrange of) it.
        """
        from_slot = posix_ms_to_slot(self.valid_from_ms)
        if slot_to_posix_ms(from_slot) < self.valid_from_ms:
            from_slot += 1
        return from_slot, posix_ms_to_slot(self.valid_to_ms)

    def contains_slot_window(self, valid_from_slot: int, valid_to_slot: int) -> bool:
        """True if the signed ms window covers the given tx slot window.

        A transaction pinned to `[valid_from_slot, valid_to_slot]` is covered only
        when both slot boundaries fall inside the signed message's ms window.
        """
        return (
            slot_to_posix_ms(valid_from_slot) >= self.valid_from_ms
            and slot_to_posix_ms(valid_to_slot) <= self.valid_to_ms
        )
