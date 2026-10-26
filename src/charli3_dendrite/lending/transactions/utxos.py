"""Resolved-UTxO plumbing shared by the lending transaction builders.

A :class:`Utxo` is a transaction output resolved from chain (or from a captured
fixture): address, value, inline datum or datum hash, reference script and out-ref.
The helpers turn it into a pycardano ``UTxO`` for the builder, into an Ogmios
``additionalUtxo`` entry for evaluation, and to and from a backend
``PoolStateInfo`` record, and find reference scripts by hash.

Reference scripts are typed by the language recorded with them
(``ref_script_type``, the dbsync script type: ``plutusV1``/``plutusV2``/
``plutusV3``, or ``timelock``/``multisig`` for a native script); an untyped script
is read as PlutusV3. A Plutus script's bytes are the single-wrapped CBOR byte
string dbsync and Ogmios return; a native script's bytes are its CBOR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from pycardano import Address
from pycardano import DatumHash
from pycardano import InvalidBefore
from pycardano import InvalidHereAfter
from pycardano import NativeScript
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import PlutusV3Script
from pycardano import RawCBOR
from pycardano import ScriptAll
from pycardano import ScriptAny
from pycardano import ScriptNofK
from pycardano import ScriptPubkey
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import plutus_script_hash

from charli3_dendrite.lending.units import DECODE_ERRORS
from charli3_dendrite.lending.units import POLICY_HEX_LEN
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from charli3_dendrite.dataclasses.models import PoolStateInfo

__all__ = [
    "SCRIPT_LANG",
    "Utxo",
    "ogmios_entry",
    "plutus_script",
    "script_hash_of",
    "script_ref_by_hash",
    "to_pycardano_utxo",
    "utxo_from_dict",
    "utxo_from_info",
    "utxo_to_info",
    "utxo_value",
]

# Ogmios ``additionalUtxo`` script-language tags, keyed by dbsync script type.
SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}

# pycardano script class per dbsync script type; an untyped script is PlutusV3.
_SCRIPT_CLASS: dict[str, type[PlutusV1Script | PlutusV2Script | PlutusV3Script]] = {
    "plutusV1": PlutusV1Script,
    "plutusV2": PlutusV2Script,
    "plutusV3": PlutusV3Script,
}

# dbsync script types of a native (phase-1) script.
_NATIVE_SCRIPT_TYPES = frozenset({"timelock", "multisig"})


@dataclass
class Utxo:
    """A resolved UTxO (transaction output or [reference] input).

    ``datum_hash`` is set only for an output that carries a datum hash and no inline
    datum.
    """

    address: str
    lovelace: int
    assets: list[tuple[str, str, int]]  # (policy_hex, name_hex, qty)
    datum: str | None
    ref_script: str | None = None
    ref_script_type: str | None = None
    out_ref: tuple[str, int] | None = None
    datum_hash: str | None = None

    def holds(self, policy: str, name: str, qty: int | None = None) -> bool:
        """True if this UTxO holds the given asset (optionally at an exact qty)."""
        return any(
            a[0] == policy and a[1] == name and (qty is None or a[2] == qty)
            for a in self.assets
        )

    def holds_policy(self, policy: str) -> bool:
        """True if this UTxO holds any asset under the given policy."""
        return any(a[0] == policy for a in self.assets)


def plutus_script(u: Utxo) -> PlutusV1Script | PlutusV2Script | PlutusV3Script:
    """The reference script ``u`` carries, typed by its recorded language.

    ``ref_script_type`` ``None`` reads as PlutusV3. Raises ``ValueError`` when the
    UTxO carries no script bytes or names a language that is not Plutus.
    """
    if not u.ref_script:
        raise ValueError("reference UTxO carries no script")
    cls = _script_class(u)
    if cls is None:
        raise ValueError(f"unsupported reference script type {u.ref_script_type!r}")
    return cls(bytes.fromhex(u.ref_script))


def _script_class(
    u: Utxo,
) -> type[PlutusV1Script | PlutusV2Script | PlutusV3Script] | None:
    """The pycardano class of ``u``'s Plutus script; None for any other type."""
    return _SCRIPT_CLASS.get(u.ref_script_type or "plutusV3")


def script_hash_of(u: Utxo) -> str:
    """The script hash (hex) of a resolved reference-script UTxO."""
    return plutus_script_hash(plutus_script(u)).payload.hex()


def utxo_value(lovelace: int, assets: list[tuple[str, str, int]]) -> Value:
    """A pycardano `Value` from a lovelace balance + (policy, name, qty) leaves."""
    from charli3_dendrite.dataclasses.models import Assets

    return asset_to_value(Assets(**_unit_quantities(lovelace, assets)))


def _unit_quantities(
    lovelace: int,
    assets: list[tuple[str, str, int]],
) -> dict[str, int]:
    """Quantity per dendrite unit; an empty (policy, name) leaf adds to lovelace."""
    root: dict[str, int] = {"lovelace": lovelace}
    for policy, name, qty in assets:
        unit = "lovelace" if not policy and not name else policy + name
        root[unit] = root.get(unit, 0) + qty
    return root


def to_pycardano_utxo(u: Utxo) -> UTxO:
    """Convert a resolved `Utxo` into a pycardano `UTxO` (datum + ref script).

    The inline-datum bytes are preserved exactly (wrapped as `RawCBOR`): these UTxOs are
    only referenced by out-ref, so the datum is not re-serialized into the tx body. A
    datum hash is carried only when there is no inline datum.

    A Plutus reference script is typed by its recorded language. A script of any other
    type (a native ``timelock``/``multisig`` script, e.g. on a wallet funding UTxO) is
    wrapped as PlutusV3: the output of a spent or referenced UTxO is never serialized
    into the body, so the wrapping only carries the script bytes and their size.
    """
    if u.out_ref is None:
        raise ValueError("cannot build a UTxO without an out-ref")
    tx_id_hex, out_idx = u.out_ref
    datum = RawCBOR(bytes.fromhex(u.datum)) if u.datum else None
    datum_hash = (
        DatumHash(bytes.fromhex(u.datum_hash)) if u.datum_hash and not u.datum else None
    )
    script = (
        (_script_class(u) or PlutusV3Script)(bytes.fromhex(u.ref_script))
        if u.ref_script
        else None
    )
    return UTxO(
        input=TransactionInput(
            transaction_id=TransactionId(bytes.fromhex(tx_id_hex)),
            index=out_idx,
        ),
        output=TransactionOutput(
            address=Address.decode(u.address),
            amount=utxo_value(u.lovelace, u.assets),
            datum_hash=datum_hash,
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

    A Plutus script is sent with its language tag (untyped reads as ``plutus:v3``); a
    native script (``timelock``/``multisig``) is sent in Ogmios's native form, its
    clause JSON plus its CBOR.

    Raises:
        ValueError: On a UTxO without an out-ref, a script type that is neither Plutus
            nor native, or native script bytes that do not decode.
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
    elif u.datum_hash:
        entry["datumHash"] = u.datum_hash
    if u.ref_script:
        entry["script"] = _ogmios_script(u.ref_script, u.ref_script_type)
    return entry


def _ogmios_script(script: str, script_type: str | None) -> dict[str, Any]:
    """The Ogmios ``Script`` object of reference-script bytes of ``script_type``."""
    if script_type in _NATIVE_SCRIPT_TYPES:
        try:
            native = NativeScript.from_cbor(script)
        except DECODE_ERRORS as exc:
            raise ValueError(
                f"reference UTxO carries an unreadable native script: {exc}",
            ) from exc
        return {"language": "native", "json": _native_json(native), "cbor": script}
    language = SCRIPT_LANG.get(script_type or "plutusV3")
    if language is None:
        raise ValueError(f"unsupported reference script type {script_type!r}")
    return {"language": language, "cbor": script}


def _native_json(script: NativeScript) -> dict[str, Any]:
    """Ogmios's clause JSON of a native script."""
    if isinstance(script, ScriptPubkey):
        return {"clause": "signature", "from": script.key_hash.payload.hex()}
    if isinstance(script, ScriptAll):
        return {
            "clause": "all",
            "from": [_native_json(s) for s in script.native_scripts],
        }
    if isinstance(script, ScriptAny):
        return {
            "clause": "any",
            "from": [_native_json(s) for s in script.native_scripts],
        }
    if isinstance(script, ScriptNofK):
        return {
            "clause": "some",
            "atLeast": script.n,
            "from": [_native_json(s) for s in script.native_scripts],
        }
    if isinstance(script, InvalidBefore):
        # Valid from slot ``before`` on.
        return {"clause": "after", "slot": script.before}
    if isinstance(script, InvalidHereAfter):
        # Valid before slot ``after``.
        return {"clause": "before", "slot": script.after}
    raise ValueError(f"unsupported native script clause {type(script).__name__}")


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
        datum_hash=d.get("datum_hash"),
    )


def utxo_from_info(info: PoolStateInfo) -> Utxo:
    """A `Utxo` from a backend UTxO record (no reference script).

    The record's units split into ``(policy, name, qty)`` in the canonical order
    (policy id, then asset name); its lovelace comes from the ``"lovelace"`` unit and
    an empty datum reads as none.
    """
    assets = sorted(
        (unit[:POLICY_HEX_LEN], unit[POLICY_HEX_LEN:], int(qty))
        for unit, qty in info.assets.items()
        if unit != "lovelace"
    )
    return Utxo(
        address=info.address,
        lovelace=int(info.assets["lovelace"]),
        assets=assets,
        datum=info.datum_cbor or None,
        out_ref=(info.tx_hash, info.tx_index),
    )


def utxo_to_info(u: Utxo) -> PoolStateInfo:
    """The backend UTxO record of ``u``: the inverse of :func:`utxo_from_info`.

    Block placement is unknown and reads as zero; the record carries no datum hash.
    """
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.dataclasses.models import PoolStateInfo

    if u.out_ref is None:
        raise ValueError("cannot build a UTxO record without an out-ref")
    root = _unit_quantities(u.lovelace, u.assets)
    tx_hash, index = u.out_ref
    return PoolStateInfo(
        address=u.address,
        tx_hash=tx_hash,
        tx_index=index,
        block_time=0,
        block_index=0,
        block_hash="",
        datum_hash="",
        datum_cbor=u.datum or "",
        assets=Assets(root=root),
        plutus_v2=True,
    )


def script_ref_by_hash(ref_inputs: list[Utxo], script_hash: str) -> Utxo:
    """The reference-input UTxO whose Plutus script hashes to `script_hash`."""
    target = bytes.fromhex(script_hash)
    for u in ref_inputs:
        if not u.ref_script or _script_class(u) is None:
            continue
        if plutus_script_hash(plutus_script(u)).payload == target:
            return u
    raise ValueError(f"no reference script for hash {script_hash}")
