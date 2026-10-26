"""Shared helpers for the Liqwid V2 builder tests: fixtures, forward builds, replays.

Captured transactions live in two gzipped JSON files next to the capture script
(``fixtures/_capture_txs.py``): ``txs.json.gz`` maps each label to its capture and
``scripts.json.gz`` maps ``"<tx hash>#<index>"`` to the reference script held at that
out-ref. The same V2 scripts back most captures, so each is stored once; a UTxO in
``txs.json.gz`` keeps its ``ref_script_type`` and a null ``ref_script``, and
:func:`fixture` puts the bytes back.

The replay views read a built transaction from its CBOR, not through pycardano's
decoder: decoding re-encodes every inline datum and redeemer (a definite list comes
back indefinite), so only the raw bytes compare exactly with a capture.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import gzip
import hashlib
import json
import os
import re
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any
from typing import NamedTuple

import cbor2
import pytest
from pycardano import Address
from pycardano import Asset
from pycardano import MultiAsset
from pycardano import RawCBOR
from pycardano import ScriptHash
from pycardano import Transaction
from pycardano import TransactionBuilder
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import script_hash

from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.datums import encode_action_datum
from charli3_dendrite.lending.liqwid.datums import encode_loan_datum
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
from charli3_dendrite.utility import posix_ms_to_slot

FIXTURES = Path(__file__).parent / "fixtures"
TXS_FILE = "txs.json.gz"
SCRIPTS_FILE = "scripts.json.gz"
# Redeemer tags as dbsync names the purposes.
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward", 4: "vote", 5: "propose"}
# The dbsync purposes Ogmios names differently.
_OGMIOS_PURPOSE = {"reward": "withdraw", "cert": "publish"}
_UTXO_LISTS = ("inputs", "outputs", "ref_inputs")
_DRIVER_DSN = re.compile(r"^postgres(?:ql)?\+[^:/]+://")

needs_ogmios = pytest.mark.skipif(
    not os.environ.get("OGMIOS_HOST"),
    reason="needs OGMIOS_HOST",
)
needs_dbsync = pytest.mark.skipif(
    not (
        (os.environ.get("DBSYNC_HOST") or os.environ.get("DBSYNC_DSN"))
        and os.environ.get("OGMIOS_HOST")
    ),
    reason="needs DBSYNC_DSN or DBSYNC_*, and OGMIOS_HOST",
)


def dbsync_connect_kwargs() -> dict[str, Any]:
    """``psycopg.connect`` keyword arguments for the dbsync the environment names.

    ``DBSYNC_DSN`` wins: surrounding quotes are dropped and a SQLAlchemy driver URL
    (``postgresql+<driver>://``) becomes a plain ``postgresql://`` conninfo. Without
    it the five ``DBSYNC_*`` parts are read, with the defaults of the other lending
    capture scripts.
    """
    dsn = os.environ.get("DBSYNC_DSN", "").strip().strip("\"'")
    if dsn:
        return {"conninfo": _DRIVER_DSN.sub("postgresql://", dsn)}
    return {
        "host": os.environ.get("DBSYNC_HOST", "localhost"),
        "port": int(os.environ.get("DBSYNC_PORT", "5432")),
        "dbname": os.environ.get("DBSYNC_DB_NAME", "cexplorer"),
        "user": os.environ.get("DBSYNC_USER", "cexplorer"),
        "password": os.environ.get("DBSYNC_PASS", ""),
        "connect_timeout": 30,
    }


# --- fixtures -------------------------------------------------------------------


def read_json_gz(path: Path) -> dict[str, Any]:
    """The JSON object in the gzipped file ``path``, or ``{}`` when there is none."""
    if not path.exists():
        return {}
    return json.loads(gzip.decompress(path.read_bytes()))


def write_json_gz(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` as compact, key-sorted, reproducible gzipped JSON."""
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))


@functools.lru_cache(maxsize=1)
def _load_txs() -> dict[str, dict[str, Any]]:
    return read_json_gz(FIXTURES / TXS_FILE)


@functools.lru_cache(maxsize=1)
def _load_scripts() -> dict[str, dict[str, Any]]:
    return read_json_gz(FIXTURES / SCRIPTS_FILE)


def _utxos(fix: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for key in _UTXO_LISTS:
        yield from fix.get(key, ())


def _script_key(utxo: dict[str, Any]) -> str:
    tx_hash, index = utxo["out_ref"]
    return f"{tx_hash}#{index}"


def detach_scripts(fix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Move every reference script out of ``fix``; the scripts by ``"<hash>#<ix>"``.

    Each UTxO keeps its ``ref_script_type``; its ``ref_script`` becomes ``None``.
    """
    scripts = {}
    for utxo in _utxos(fix):
        if utxo.get("ref_script"):
            scripts[_script_key(utxo)] = {
                "ref_script": utxo["ref_script"],
                "ref_script_type": utxo.get("ref_script_type"),
            }
            utxo["ref_script"] = None
    return scripts


def attach_scripts(fix: dict[str, Any], scripts: dict[str, dict[str, Any]]) -> None:
    """Put the reference scripts of :func:`detach_scripts` back into ``fix``."""
    for utxo in _utxos(fix):
        if utxo.get("ref_script") or not utxo.get("out_ref"):
            continue
        script = scripts.get(_script_key(utxo))
        if script is not None:
            utxo["ref_script"] = script["ref_script"]
            utxo["ref_script_type"] = script["ref_script_type"]


def fixture(label: str) -> dict[str, Any]:
    """A captured transaction, its reference scripts re-attached (a fresh copy)."""
    txs = _load_txs()
    if label not in txs:
        raise KeyError(f"no captured transaction {label!r}")
    fix = copy.deepcopy(txs[label])
    attach_scripts(fix, _load_scripts())
    return fix


def labels(prefix: str = "") -> list[str]:
    """The sorted labels of the captured transactions starting with ``prefix``."""
    return sorted(label for label in _load_txs() if label.startswith(prefix))


def fixture_utxos(fix: dict[str, Any]) -> list[Utxo]:
    """A capture's spent inputs and reference inputs."""
    return [utxo_from_dict(u) for u in fix["inputs"] + fix["ref_inputs"]]


def ogmios_entries(utxos: Iterable[Utxo]) -> list[dict[str, Any]]:
    """Ogmios ``additionalUtxo`` entries for ``utxos``, one per out-ref."""
    entries: dict[tuple[str, int] | None, dict[str, Any]] = {}
    for utxo in utxos:
        entries.setdefault(utxo.out_ref, ogmios_entry(utxo))
    return list(entries.values())


# --- snapshots ------------------------------------------------------------------


def block_slot(fix: dict[str, Any]) -> int:
    """The slot of the block a capture landed in."""
    return posix_ms_to_slot(fix["block_time"] * 1000)


def validator_credentials(snapshot: Any) -> set[str]:  # noqa: ANN401
    """The payment credentials of a snapshot market's action and loan validators."""
    params = snapshot.records.params_datum
    return {params.action_validator, params.loan_validator}


def with_params(snapshot: Any, **changes: Any) -> Any:  # noqa: ANN401
    """``snapshot`` with its market's MarketParams fields replaced by ``changes``."""
    records = snapshot.records
    params = dataclasses.replace(records.params_datum, **changes)
    record = dataclasses.replace(records.params, datum=params)
    return dataclasses.replace(
        snapshot,
        records=dataclasses.replace(records, params=record),
    )


def with_feed_price(
    snapshot: Any,  # noqa: ANN401
    underlying: str,
    price: Fraction,
) -> Any:  # noqa: ANN401
    """``snapshot`` with the USD price of ``underlying``'s health feed replaced."""
    feeds = dict(snapshot.health.feeds)
    feeds[underlying] = dataclasses.replace(
        feeds[underlying],
        usd_num=price.numerator,
        usd_den=price.denominator,
    )
    return dataclasses.replace(
        snapshot,
        health=dataclasses.replace(snapshot.health, feeds=feeds),
    )


# --- forward builds -------------------------------------------------------------


@dataclass
class Built:
    """An assembled unsigned transaction: its CBOR as built, and decoded."""

    cbor: str

    @property
    def tx(self) -> Transaction:
        """The decoded transaction (for inspection only; compare through the views)."""
        return Transaction.from_cbor(self.cbor)


def build(
    contribute: Callable[..., None],
    snapshot: object,
    *,
    slot: int,
) -> Built:
    """Forward-build ``snapshot`` into an unsigned transaction."""
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=slot))
    contribute(tx_builder, snapshot=snapshot)
    return Built(assemble_unsigned(tx_builder))


# --- Ogmios evaluation ----------------------------------------------------------


def evaluate(built: Built, utxos: Iterable[Utxo]) -> list[str]:
    """Ogmios-evaluate ``built`` with ``utxos`` supplied; the sorted redeemer purposes.

    Every script must succeed with a positive budget. An entry Ogmios still holds
    on its ledger is dropped and resolved there (the seam's retry).
    """
    budgets = evaluate_tx_cbor(built.cbor, ogmios_entries(utxos))
    for budget in budgets:
        assert budget["budget"]["memory"] > 0
        assert budget["budget"]["cpu"] > 0
    return sorted(b["validator"]["purpose"] for b in budgets)


def captured_purposes(fix: dict[str, Any]) -> list[str]:
    """The sorted redeemer purposes of a capture, as Ogmios names them."""
    return sorted(
        _OGMIOS_PURPOSE.get(r["purpose"], r["purpose"]) for r in fix["redeemers"]
    )


def synthetic_utxo(
    seed: str,
    *,
    address: str,
    lovelace: int,
    assets: Iterable[tuple[str, str, int]] = (),
    datum: str | None = None,
) -> Utxo:
    """A UTxO at an out-ref no ledger holds, derived from ``seed``.

    Ogmios resolves it only from ``additionalUtxo``, so a changed copy of a record
    or a made-up wallet input is evaluated as given, whatever the ledger holds.
    """
    tx_hash = hashlib.blake2b(seed.encode(), digest_size=32).hexdigest()
    return Utxo(
        address=address,
        lovelace=lovelace,
        assets=list(assets),
        datum=datum,
        out_ref=(tx_hash, 0),
    )


# --- mutations ------------------------------------------------------------------

# A change applied to a builder after a flow's legs, before assembly.
Mutation = Callable[[TransactionBuilder], None]


def mutated(
    contribute: Callable[..., None],
    *mutations: Mutation,
) -> Callable[..., None]:
    """``contribute`` followed by ``mutations``, in order (a :func:`build` argument)."""

    def run(tx_builder: TransactionBuilder, *, snapshot: object) -> None:
        contribute(tx_builder, snapshot=snapshot)
        for mutation in mutations:
            mutation(tx_builder)

    return run


def _credential_of(output: TransactionOutput) -> str | None:
    payment = output.address.payment_part
    return None if payment is None else payment.payload.hex()


def output_index(tx_builder: TransactionBuilder, credential: str) -> int:
    """The index of the one output at payment ``credential``."""
    (index,) = (
        i
        for i, output in enumerate(tx_builder.outputs)
        if _credential_of(output) == credential
    )
    return index


def _datum_bytes(output: TransactionOutput) -> bytes:
    datum = output.datum
    assert isinstance(datum, RawCBOR), f"no inline datum on {output}"
    return bytes(datum.cbor)


def _with_value(
    output: TransactionOutput,
    lovelace: int,
    multi_asset: MultiAsset,
) -> TransactionOutput:
    return TransactionOutput(
        output.address,
        Value(lovelace, multi_asset),
        datum=output.datum,
    )


def change_datum(credential: str, change: Callable[[bytes], bytes]) -> Mutation:
    """Rewrite the inline datum of the output at ``credential`` with ``change``."""

    def run(tx_builder: TransactionBuilder) -> None:
        index = output_index(tx_builder, credential)
        output = tx_builder.outputs[index]
        tx_builder.outputs[index] = TransactionOutput(
            output.address,
            output.amount,
            datum=RawCBOR(change(_datum_bytes(output))),
        )

    return run


def shift_loan_datum(credential: str, **shifts: int) -> Mutation:
    """Add ``shifts`` (LoanDatum field -> amount) to the loan output's record."""

    def change(datum: bytes) -> bytes:
        loan = decode_loan(datum)
        fields = {name: getattr(loan, name) + by for name, by in shifts.items()}
        indefinite = datum_style(datum) == "indefinite"
        return encode_loan_datum(
            dataclasses.replace(loan, **fields),
            indefinite=indefinite,
        )

    return change_datum(credential, change)


def shift_action_datum(credential: str, **shifts: int) -> Mutation:
    """Add ``shifts`` (ActionDatum field -> amount) to the action output's record."""

    def change(datum: bytes) -> bytes:
        action = decode_action(datum)
        fields = {name: getattr(action, name) + by for name, by in shifts.items()}
        indefinite = datum_style(datum) == "indefinite"
        return encode_action_datum(
            dataclasses.replace(action, **fields),
            indefinite=indefinite,
        )

    return change_datum(credential, change)


def shift_value(
    credential: str,
    *,
    lovelace: int = 0,
    assets: dict[str, int] | None = None,
) -> Mutation:
    """Add ``lovelace`` and ``assets`` (unit -> quantity) to the output at ``credential``."""

    def run(tx_builder: TransactionBuilder) -> None:
        index = output_index(tx_builder, credential)
        output = tx_builder.outputs[index]
        multi_asset = output.amount.multi_asset
        for unit, qty in (assets or {}).items():
            multi_asset = multi_asset + MultiAsset.from_primitive(
                {bytes.fromhex(unit[:56]): {bytes.fromhex(unit[56:]): qty}},
            )
        tx_builder.outputs[index] = _with_value(
            output,
            output.amount.coin + lovelace,
            multi_asset,
        )

    return run


def set_upper_bound_ms(upper_ms: int | None) -> Mutation:
    """Replace the upper validity bound by the slot holding ``upper_ms`` (None: none)."""

    def run(tx_builder: TransactionBuilder) -> None:
        tx_builder.ttl = None if upper_ms is None else posix_ms_to_slot(upper_ms)

    return run


def drop_required_signers(tx_builder: TransactionBuilder) -> None:
    """Require no signature."""
    tx_builder.required_signers = None


def add_reference(utxo: Utxo) -> Mutation:
    """Reference ``utxo`` as well."""

    def run(tx_builder: TransactionBuilder) -> None:
        tx_builder.reference_inputs.add(to_pycardano_utxo(utxo))

    return run


def _reference_ref(member: UTxO | TransactionInput) -> tuple[str, int]:
    tx_input = member.input if isinstance(member, UTxO) else member
    return bytes(tx_input.transaction_id).hex(), tx_input.index


def replace_reference(out_ref: tuple[str, int], utxo: Utxo | None) -> Mutation:
    """Stop referencing ``out_ref``; reference ``utxo`` in its place when given."""

    def run(tx_builder: TransactionBuilder) -> None:
        (member,) = (
            m for m in tx_builder.reference_inputs if _reference_ref(m) == out_ref
        )
        tx_builder.reference_inputs.discard(member)
        if utxo is not None:
            tx_builder.reference_inputs.add(to_pycardano_utxo(utxo))

    return run


def keep_loan_token(policy: str) -> Mutation:
    """Undo the loan-token change: no mint of ``policy``, the loan keeps its name.

    The burnt name replaces the minted one in every output and the policy's
    script leaves the mint.
    """

    def run(tx_builder: TransactionBuilder) -> None:
        key = ScriptHash(bytes.fromhex(policy))
        assert tx_builder.mint is not None
        tokens = tx_builder.mint[key]
        (burnt,) = (name for name, qty in tokens.items() if qty == -1)
        (minted,) = (name for name, qty in tokens.items() if qty == 1)
        rest = {k: v for k, v in tx_builder.mint.items() if k != key}
        tx_builder.mint = MultiAsset(rest) if rest else None
        tx_builder._minting_script_to_redeemers = [
            (script, redeemer)
            for script, redeemer in tx_builder._minting_script_to_redeemers
            if script_hash(script) != key
        ]
        for index, output in enumerate(tx_builder.outputs):
            held = output.amount.multi_asset.get(key)
            if held is None or minted not in held:
                continue
            renamed = MultiAsset(dict(output.amount.multi_asset))
            renamed[key] = Asset({burnt: 1})
            tx_builder.outputs[index] = _with_value(
                output,
                output.amount.coin,
                renamed,
            )

    return run


# --- raw CBOR -------------------------------------------------------------------


def _head(data: bytes, at: int) -> tuple[int, int | None, int]:
    """``(major type, argument or None when indefinite, offset after the head)``."""
    initial = data[at]
    major, info = initial >> 5, initial & 0x1F
    at += 1
    if info < 24:
        return major, info, at
    if info == 31:
        return major, None, at
    size = 1 << (info - 24)
    return major, int.from_bytes(data[at : at + size], "big"), at + size


def _end(data: bytes, at: int) -> int:
    """The offset just past the CBOR item starting at ``at``."""
    major, arg, at = _head(data, at)
    if major in (0, 1, 7):
        return at
    if major in (2, 3):
        if arg is not None:
            return at + arg
        while data[at] != 0xFF:
            at = _end(data, at)
        return at + 1
    if major == 6:
        return _end(data, at)
    count = None if arg is None else arg * (2 if major == 5 else 1)
    seen = 0
    while (data[at] != 0xFF) if count is None else (seen < count):
        at = _end(data, at)
        seen += 1
    return at + 1 if count is None else at


def _items(data: bytes, at: int) -> Iterator[tuple[int, int]]:
    """The ``(start, end)`` spans of the items of the array or map at ``at``."""
    major, arg, at = _head(data, at)
    if major == 6:  # a tagged set: its items are the array's
        yield from _items(data, at)
        return
    count = None if arg is None else arg * (2 if major == 5 else 1)
    seen = 0
    while (data[at] != 0xFF) if count is None else (seen < count):
        end = _end(data, at)
        yield at, end
        at = end
        seen += 1


def _map_span(data: bytes, at: int, key: int) -> tuple[int, int] | None:
    """The span of the value under integer ``key`` in the map at ``at``."""
    spans = list(_items(data, at))
    for (k_start, k_end), value in zip(spans[::2], spans[1::2]):
        if cbor2.loads(data[k_start:k_end]) == key:
            return value
    return None


def _raw(built: Built) -> bytes:
    return bytes.fromhex(built.cbor)


def _body(built: Built) -> dict[int, Any]:
    return cbor2.loads(_raw(built))[0]


def _elements(value: Any) -> list[Any]:  # noqa: ANN401
    """The members of a CBOR array, or of a tag-258 set."""
    if value is None:
        return []
    if isinstance(value, cbor2.CBORTag):
        value = value.value
    return list(value)


def _units(multi_asset: dict[bytes, dict[bytes, int]]) -> set[tuple[str, int]]:
    return {
        (policy.hex() + name.hex(), qty)
        for policy, names in multi_asset.items()
        for name, qty in names.items()
    }


# --- views ----------------------------------------------------------------------


class OutputView(NamedTuple):
    """An output as a replay compares it."""

    address: str
    lovelace: int
    assets: frozenset[tuple[str, int]]
    datum: str | None
    datum_hash: str | None


def _sorted_views(views: Iterable[OutputView]) -> list[OutputView]:
    return sorted(
        views,
        key=lambda v: (
            v.address,
            v.lovelace,
            sorted(v.assets),
            v.datum or "",
            v.datum_hash or "",
        ),
    )


def _payment_credential(address: str) -> str | None:
    try:
        payment = Address.decode(address).payment_part
    except Exception:  # noqa: BLE001 - Byron and malformed addresses carry none
        return None
    return None if payment is None else payment.payload.hex()


def redeemers(built: Built) -> set[tuple[str, int, str]]:
    """``(purpose, index, CBOR hex)`` of every redeemer of ``built``, bytes as built."""
    data = _raw(built)
    witness = list(_items(data, 0))[1][0]
    span = _map_span(data, witness, 5)
    if span is None:
        return set()
    out = set()
    if data[span[0]] >> 5 == 5:  # map form: {[tag, index]: [data, ex_units]}
        spans = list(_items(data, span[0]))
        for (k_start, k_end), (v_start, _) in zip(spans[::2], spans[1::2]):
            tag, index = cbor2.loads(data[k_start:k_end])
            d_start, d_end = next(_items(data, v_start))
            out.add((_PURPOSE[tag], index, data[d_start:d_end].hex()))
    else:  # array form: [[tag, index, data, ex_units]]
        for r_start, _ in _items(data, span[0]):
            tag_span, index_span, (d_start, d_end), _ex_units = _items(data, r_start)
            tag = cbor2.loads(data[tag_span[0] : tag_span[1]])
            index = cbor2.loads(data[index_span[0] : index_span[1]])
            out.add((_PURPOSE[tag], index, data[d_start:d_end].hex()))
    return out


def captured_redeemers(fix: dict[str, Any]) -> set[tuple[str, int, str]]:
    """``(purpose, index, CBOR hex)`` of every redeemer of a capture."""
    return {(r["purpose"], int(r["index"]), r["cbor"]) for r in fix["redeemers"]}


def _built_output(output: Any) -> OutputView:  # noqa: ANN401
    datum = datum_hash = None
    address, value = output[0], output[1]
    if isinstance(output, list):  # legacy: [address, value, datum hash?]
        if len(output) > 2:
            datum_hash = output[2].hex()
    else:  # {0: address, 1: value, 2: datum option, 3: script reference}
        option = output.get(2)
        if option is not None and option[0] == 0:
            datum_hash = option[1].hex()
        elif option is not None:
            datum = option[1].value.hex()  # tag 24: the datum bytes as built
    lovelace, assets = (value, {}) if isinstance(value, int) else value
    return OutputView(
        str(Address.from_primitive(address)),
        lovelace,
        frozenset(_units(assets)),
        datum,
        datum_hash,
    )


def protocol_outputs(built: Built, credentials: set[str]) -> list[OutputView]:
    """The outputs of ``built`` at a payment credential in ``credentials``, sorted."""
    views = (_built_output(o) for o in _body(built).get(1) or [])
    return _sorted_views(
        v for v in views if _payment_credential(v.address) in credentials
    )


def captured_protocol_outputs(
    fix: dict[str, Any],
    credentials: set[str],
) -> list[OutputView]:
    """:func:`protocol_outputs` of a capture."""
    views = (
        OutputView(
            o["address"],
            int(o["lovelace"]),
            frozenset((p + n, int(q)) for p, n, q in o["assets"]),
            o.get("datum"),
            o.get("datum_hash"),
        )
        for o in fix["outputs"]
    )
    return _sorted_views(
        v for v in views if _payment_credential(v.address) in credentials
    )


def mints(built: Built) -> set[tuple[str, int]]:
    """``(unit, quantity)`` of every asset ``built`` mints (negative: burns)."""
    return _units(_body(built).get(9) or {})


def captured_mints(fix: dict[str, Any]) -> set[tuple[str, int]]:
    """:func:`mints` of a capture."""
    return {(p + n, int(q)) for p, n, q in fix["mints"]}


def withdrawals(built: Built) -> set[tuple[str, int]]:
    """``(stake credential hex, amount)`` of every withdrawal of ``built``."""
    return {
        (bytes(address)[1:].hex(), amount)
        for address, amount in (_body(built).get(5) or {}).items()
    }


def captured_withdrawals(fix: dict[str, Any]) -> set[tuple[str, int]]:
    """:func:`withdrawals` of a capture."""
    return {(credential, int(amount)) for credential, amount in fix["withdrawals"]}


def required_signers(built: Built) -> list[str]:
    """The sorted key hashes ``built`` requires to sign."""
    return sorted(bytes(h).hex() for h in _elements(_body(built).get(14)))


def captured_required_signers(fix: dict[str, Any]) -> list[str]:
    """:func:`required_signers` of a capture."""
    return sorted(fix["required_signers"])


def reference_inputs(built: Built) -> set[tuple[str, int]]:
    """The out-refs ``built`` reads as reference inputs."""
    return {
        (bytes(tx_id).hex(), index) for tx_id, index in _elements(_body(built).get(18))
    }


def captured_reference_inputs(fix: dict[str, Any]) -> set[tuple[str, int]]:
    """:func:`reference_inputs` of a capture."""
    return {(u["out_ref"][0], int(u["out_ref"][1])) for u in fix["ref_inputs"]}


def validity(built: Built) -> tuple[int | None, int | None]:
    """``(invalid before, invalid hereafter)`` slots of ``built``."""
    body = _body(built)
    return body.get(8), body.get(3)


def captured_validity(fix: dict[str, Any]) -> tuple[int | None, int | None]:
    """:func:`validity` of a capture."""
    return fix["invalid_before"], fix["invalid_hereafter"]


def assert_replays(
    built: Built,
    fix: dict[str, Any],
    *,
    credentials: set[str],
) -> None:
    """Assert ``built`` replays the capture ``fix``, naming every view that differs.

    Compared: redeemers, the outputs at ``credentials``, mints, withdrawals, required
    signers, reference inputs and the validity interval.
    """
    views: list[tuple[str, Any, Any]] = [
        ("redeemers", redeemers(built), captured_redeemers(fix)),
        (
            "protocol outputs",
            protocol_outputs(built, credentials),
            captured_protocol_outputs(fix, credentials),
        ),
        ("mints", mints(built), captured_mints(fix)),
        ("withdrawals", withdrawals(built), captured_withdrawals(fix)),
        ("required signers", required_signers(built), captured_required_signers(fix)),
        ("reference inputs", reference_inputs(built), captured_reference_inputs(fix)),
        ("validity", validity(built), captured_validity(fix)),
    ]
    differing = [
        f"{name}:\n  built    {got!r}\n  captured {want!r}"
        for name, got, want in views
        if got != want
    ]
    assert not differing, f"{fix['label']} does not replay:\n" + "\n".join(differing)
