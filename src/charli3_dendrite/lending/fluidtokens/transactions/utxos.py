"""Resolved-UTxO plumbing shared by the FluidTokens transaction builders.

A :class:`Utxo` is a transaction output resolved from chain (or from a captured
fixture): address, value, inline datum, reference script and out-ref. The helpers turn
it into a pycardano ``UTxO`` for the builder, into an Ogmios ``additionalUtxo`` entry
for evaluation, and find reference scripts by hash. Nothing here depends on a protocol
version.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from pycardano import Address
from pycardano import PlutusV3Script
from pycardano import RawCBOR
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import plutus_script_hash

from charli3_dendrite.lending.fluidtokens.datums import TxOutRef
from charli3_dendrite.utility import asset_to_value

# Ogmios ``additionalUtxo`` script-language tags, keyed by dbsync script type.
SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


@dataclass
class Utxo:
    """A resolved UTxO (transaction output or [reference] input)."""

    address: str
    lovelace: int
    assets: list[tuple[str, str, int]]  # (policy_hex, name_hex, qty)
    datum: str | None
    ref_script: str | None = None
    ref_script_type: str | None = None
    out_ref: tuple[str, int] | None = None

    def holds(self, policy: str, name: str, qty: int | None = None) -> bool:
        """True if this UTxO holds the given asset (optionally at an exact qty)."""
        return any(
            a[0] == policy and a[1] == name and (qty is None or a[2] == qty)
            for a in self.assets
        )

    def holds_policy(self, policy: str) -> bool:
        """True if this UTxO holds any asset under the given policy."""
        return any(a[0] == policy for a in self.assets)


def utxo_value(lovelace: int, assets: list[tuple[str, str, int]]) -> Value:
    """A pycardano `Value` from a lovelace balance + (policy, name, qty) leaves."""
    from charli3_dendrite.dataclasses.models import Assets

    root: dict[str, int] = {"lovelace": lovelace}
    for policy, name, qty in assets:
        unit = "lovelace" if not policy and not name else policy + name
        root[unit] = root.get(unit, 0) + qty
    return asset_to_value(Assets(**root))


def to_pycardano_utxo(u: Utxo) -> UTxO:
    """Convert a resolved `Utxo` into a pycardano `UTxO` (inline datum + ref script).

    The inline-datum bytes are preserved exactly (wrapped as `RawCBOR`): these UTxOs are
    only referenced by out-ref, so the datum is not re-serialized into the tx body.
    """
    if u.out_ref is None:
        raise ValueError("cannot build a UTxO without an out-ref")
    tx_id_hex, out_idx = u.out_ref
    datum = RawCBOR(bytes.fromhex(u.datum)) if u.datum else None
    script = PlutusV3Script(bytes.fromhex(u.ref_script)) if u.ref_script else None
    return UTxO(
        input=TransactionInput(
            transaction_id=TransactionId(bytes.fromhex(tx_id_hex)),
            index=out_idx,
        ),
        output=TransactionOutput(
            address=Address.decode(u.address),
            amount=utxo_value(u.lovelace, u.assets),
            datum=datum,
            script=script,
        ),
    )


def ogmios_entry(u: Utxo) -> dict[str, Any]:
    """Serialize any resolved `Utxo` into its Ogmios ``additionalUtxo`` entry.

    Used both for funding (actor) inputs when building a tx and, in the e2e replays, for
    spent/reference inputs. Funding inputs may already be spent / freshly created, so
    Ogmios cannot resolve them from its own ledger snapshot; they are supplied here for
    evaluation.
    """
    if u.out_ref is None:
        raise ValueError("cannot build an additionalUtxo entry without an out-ref")
    value: dict[str, Any] = {"ada": {"lovelace": u.lovelace}}
    for policy, name, qty in u.assets:
        value.setdefault(policy, {})[name] = qty
    entry: dict[str, Any] = {
        "transaction": {"id": u.out_ref[0]},
        "index": u.out_ref[1],
        "address": u.address,
        "value": value,
    }
    if u.datum:
        entry["datum"] = u.datum
    if u.ref_script:
        entry["script"] = {
            "language": SCRIPT_LANG.get(u.ref_script_type or "", "plutus:v3"),
            "cbor": u.ref_script,
        }
    return entry


def utxo_from_dict(d: dict) -> Utxo:
    """A `Utxo` from a captured-fixture / dbsync record dict."""
    return Utxo(
        address=d["address"],
        lovelace=int(d["lovelace"]),
        assets=[(p, n, int(q)) for p, n, q in d["assets"]],
        datum=d.get("datum"),
        ref_script=d.get("ref_script"),
        ref_script_type=d.get("ref_script_type"),
        out_ref=tuple(d["out_ref"]) if d.get("out_ref") else None,
    )


def script_ref_by_hash(ref_inputs: list[Utxo], script_hash: str) -> Utxo:
    """The reference-input UTxO whose PlutusV3 script hashes to `script_hash`."""
    target = bytes.fromhex(script_hash)
    for u in ref_inputs:
        if not u.ref_script:
            continue
        h = plutus_script_hash(PlutusV3Script(bytes.fromhex(u.ref_script))).payload
        if h == target:
            return u
    raise ValueError(f"no reference script for hash {script_hash}")


def loan_id_from_out_ref(out_ref: tuple[str, int]) -> bytes:
    """The loan id minted from an origin out-ref: ``blake2b_224`` of its CBOR.

    The bond policy names each minted NFT (loan / borrower bond / lender bond) after
    this id, derived by hashing the serialized ``OutputReference`` of the spent pool (or
    request) UTxO. The out-ref is encoded exactly as the bond-mint redeemer encodes it
    (a ``TxOutRef`` ``Constr0([tx_id, index])``) so the derived name matches the mint.
    """
    tx_ref = TxOutRef(tx_id=bytes.fromhex(out_ref[0]), index=out_ref[1])
    return hashlib.blake2b(tx_ref.to_cbor(), digest_size=28).digest()
