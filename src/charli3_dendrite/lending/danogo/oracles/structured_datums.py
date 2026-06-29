"""Structured ``Constr0`` oracle config datums (the newer Danogo oracle deployment).

The newer Float oracle deployment stores its global source registry and per-supply
routing config as structured Plutus ``Constr`` data, not the hand-packed byte blobs
the original deployment uses (`aggregator_datums.OracleGlobalConfig` /
`aggregator_datums.OraclePathDatum`). Both structured datums decode to typed views and
re-encode to byte-identical CBOR.

On-chain CBOR conventions (verified byte-exact against the live config datums):

* every non-empty array, and every ``Constr`` payload with fields, is indefinite-length
  (``0x9f … 0xff``);
* the price-path map is indefinite-length (``0xbf … 0xff``) and each ``(policy, name)``
  map key is itself an indefinite-length array;
* an empty array / nullary ``Constr`` payload is a definite empty array
  (``tag + 0x80``);
* byte strings and integers use the standard definite encodings.

`encode_plutus` reproduces those rules so a decoded datum round-trips bit-for-bit.

Global datum: ``Constr0[ sources, deviation_bps ]`` where ``sources`` is the leaf
source registry (each entry a ``Constr`` whose alternative index is the source *kind*
and whose fields carry the on-chain locator) and ``deviation_bps`` is the aggregation
tolerance. A price-path hop indexes into ``sources``.

Path datum: ``Constr0[ supply_token, paths ]`` where ``supply_token`` is the
``[policy, name]`` this config prices toward and ``paths`` maps each priceable
``(policy, name)`` token to a list of alternative derivation paths; each path is a list
of ``(source_index, is_reverse)`` hops (``is_reverse`` is ``Constr1`` vs ``Constr0``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import cbor2  # type: ignore[import-not-found]

from charli3_dendrite.lending.danogo.datums import asset_unit
from charli3_dendrite.lending.danogo.oracles.redeemer import _alt_constr_tag
from charli3_dendrite.lending.danogo.oracles.redeemer import _constr_alt

_CONSTR0 = 0
_GLOBAL_FIELDS = 2
_PATH_FIELDS = 2
_TOKEN_PAIR_LEN = 2
_POLICY_LEN = 28
# A forward hop is ``Constr0``; a reversed hop (invert the leaf rate) is ``Constr1``.
_HOP_REVERSE_ALT = 1


def _tag_prefix(tag: int) -> bytes:
    """The CBOR header bytes for a tag (without any payload)."""
    return cbor2.dumps(cbor2.CBORTag(tag, 0))[:-1]


def encode_plutus(obj: object) -> bytes:
    """Encode a decoded Plutus value to the Danogo oracle on-chain CBOR conventions.

    Mirrors how the validator serializes its config datums: non-empty arrays and
    ``Constr`` payloads are indefinite-length, maps are indefinite-length, empty
    arrays / nullary ``Constr``s are a definite empty array, and byte strings / ints
    use the standard definite encodings. Round-trips the live global and path datums
    bit-for-bit.
    """
    if isinstance(obj, cbor2.CBORTag):
        return _tag_prefix(obj.tag) + encode_plutus(obj.value)
    if isinstance(obj, (list, tuple)):
        if not obj:
            return b"\x80"
        return b"\x9f" + b"".join(encode_plutus(x) for x in obj) + b"\xff"
    if isinstance(obj, dict):
        if not obj:
            return b"\xa0"
        body = b"".join(encode_plutus(k) + encode_plutus(v) for k, v in obj.items())
        return b"\xbf" + body + b"\xff"
    return cbor2.dumps(obj)


@dataclass(frozen=True)
class StructuredOracleSource:
    """One ``oracle_sources`` entry: its source *kind* plus the raw constr fields.

    ``kind`` is the ``Constr`` alternative index, the source-kind discriminator a
    price-path hop's ``source_index`` resolves to. ``fields`` keeps the decoded
    positional fields so the entry re-encodes byte-exact. `locator` extracts the
    ``(policy, name)`` on-chain token a hop reads when the kind names one; kinds that
    instead carry an opaque feed id or a pure constant ratio return ``None``.
    """

    kind: int
    fields: Tuple[object, ...]

    @classmethod
    def from_value(cls, tag: cbor2.CBORTag) -> StructuredOracleSource:
        """Build a source descriptor from its decoded ``Constr`` value."""
        if not isinstance(tag, cbor2.CBORTag):
            raise ValueError("oracle source entry is not a Constr")
        return cls(kind=_constr_alt(tag.tag), fields=tuple(tag.value))

    def to_value(self) -> cbor2.CBORTag:
        """The decoded ``Constr`` value this descriptor re-encodes to."""
        return cbor2.CBORTag(_alt_constr_tag(self.kind), list(self.fields))

    def locator(self) -> tuple[str, str] | None:
        """The ``(policy_hex, name_hex)`` token this source names, or ``None``.

        Most source kinds carry their locator as the first field — either a bare
        ``[policy, name]`` array or a ``Constr0[policy, name]`` wrapper — naming the
        on-chain UTxO (Danogo pool, Indigo position, DEX LP, …) whose datum yields the
        leaf rate. Kinds whose first field is an opaque feed hash or a constant ratio
        have no token locator and return ``None``.
        """
        return self.token_at(0)

    def token_at(self, index: int) -> tuple[str, str] | None:
        """The ``(policy_hex, name_hex)`` token named by field ``index``, or ``None``.

        A field names a token when it is a ``[policy, name]`` array (bare or wrapped in
        a ``Constr0``) whose policy is empty (ADA) or 28 bytes. Sources that name two
        UTxOs (e.g. a Liqwid market's STATE leaf in field 0 and its PARAM leaf in field
        1) expose the second via ``index=1``. Fields carrying an opaque hash or a
        constant ratio return ``None``.
        """
        if not 0 <= index < len(self.fields):
            return None
        head = self.fields[index]
        if isinstance(head, cbor2.CBORTag):
            head = head.value
        if (
            isinstance(head, (list, tuple))
            and len(head) == _TOKEN_PAIR_LEN
            and isinstance(head[0], bytes)
            and isinstance(head[1], bytes)
            and len(head[0]) in (0, _POLICY_LEN)
        ):
            return head[0].hex(), head[1].hex()
        return None


@dataclass(frozen=True)
class StructuredOracleGlobalConfig:
    """The newer deployment's global source registry: ``Constr0[sources, deviation]``.

    ``sources`` is the indexed leaf source registry (a price-path hop's
    ``source_index`` indexes it) and ``deviation_bps`` is the aggregation deviation
    tolerance. Decodes byte-exact and re-encodes to the original datum bytes.
    """

    sources: tuple[StructuredOracleSource, ...]
    deviation_bps: int

    @classmethod
    def from_cbor(cls, data: str | bytes) -> StructuredOracleGlobalConfig:
        """Parse the global config datum (a ``Constr0`` of two fields)."""
        raw = bytes.fromhex(data) if isinstance(data, str) else data
        top = cbor2.loads(raw)
        if not isinstance(top, cbor2.CBORTag) or _constr_alt(top.tag) != _CONSTR0:
            raise ValueError("global oracle config is not a Constr0")
        fields = top.value
        if len(fields) != _GLOBAL_FIELDS:
            raise ValueError("global oracle config must have two fields")
        source_list, deviation = fields[0], fields[1]
        if not isinstance(source_list, (list, tuple)):
            raise ValueError("global oracle config sources is not a list")
        if not isinstance(deviation, int):
            raise ValueError("global oracle config deviation is not an integer")
        sources = tuple(StructuredOracleSource.from_value(s) for s in source_list)
        return cls(sources=sources, deviation_bps=int(deviation))

    def to_cbor(self) -> bytes:
        """Re-emit the exact on-chain global config CBOR (round-trips `from_cbor`)."""
        top = cbor2.CBORTag(
            _alt_constr_tag(_CONSTR0),
            [[s.to_value() for s in self.sources], self.deviation_bps],
        )
        return encode_plutus(top)


@dataclass(frozen=True)
class StructuredPathEntry:
    """One priceable token's derivation paths in a structured path config.

    ``policy`` / ``name`` identify the collateral token; ``paths`` are its alternative
    derivation paths (averaged + deviation-checked), each a tuple of
    ``(source_index, is_reverse)`` hops referencing the global config's ``sources``.
    """

    policy: bytes
    name: bytes
    paths: Tuple[Tuple[Tuple[int, bool], ...], ...]

    @property
    def unit(self) -> str:
        """The token's dendrite unit string (``'lovelace'`` for ADA)."""
        return asset_unit(self.policy, self.name)


@dataclass(frozen=True)
class StructuredOraclePathConfig:
    """The newer deployment's per-supply routing config: ``Constr0[supply, paths]``.

    ``supply_policy`` / ``supply_name`` are the supply token this config prices toward
    (the disambiguator that selects it among the deployment's path configs);
    ``entries`` are the per-collateral derivation paths. Decodes byte-exact and
    re-encodes to the original datum bytes.
    """

    supply_policy: bytes
    supply_name: bytes
    entries: tuple[StructuredPathEntry, ...]

    @property
    def supply_unit(self) -> str:
        """The supply token's dendrite unit string (``'lovelace'`` for ADA)."""
        return asset_unit(self.supply_policy, self.supply_name)

    @classmethod
    def from_cbor(cls, data: str | bytes) -> StructuredOraclePathConfig:
        """Parse the path config datum (a ``Constr0`` of supply token + paths map)."""
        raw = bytes.fromhex(data) if isinstance(data, str) else data
        top = cbor2.loads(raw)
        if not isinstance(top, cbor2.CBORTag) or _constr_alt(top.tag) != _CONSTR0:
            raise ValueError("path oracle config is not a Constr0")
        fields = top.value
        if len(fields) != _PATH_FIELDS:
            raise ValueError("path oracle config must have two fields")
        supply, paths_map = fields[0], fields[1]
        if not isinstance(supply, (list, tuple)) or len(supply) != _TOKEN_PAIR_LEN:
            raise ValueError("path oracle config supply token is not a [policy, name]")
        if not isinstance(paths_map, dict):
            raise ValueError("path oracle config paths is not a map")
        entries = tuple(
            StructuredPathEntry(
                policy=_pair_bytes(key, 0),
                name=_pair_bytes(key, 1),
                paths=_decode_paths(value),
            )
            for key, value in paths_map.items()
        )
        return cls(
            supply_policy=_as_bytes(supply[0]),
            supply_name=_as_bytes(supply[1]),
            entries=entries,
        )

    def to_cbor(self) -> bytes:
        """Re-emit the exact on-chain path config CBOR (round-trips `from_cbor`)."""
        paths_map: dict[tuple[bytes, bytes], list] = {}
        for entry in self.entries:
            paths_map[(entry.policy, entry.name)] = [
                [[idx, _hop_constr(is_reverse)] for idx, is_reverse in path]
                for path in entry.paths
            ]
        top = cbor2.CBORTag(
            _alt_constr_tag(_CONSTR0),
            [[self.supply_policy, self.supply_name], paths_map],
        )
        return encode_plutus(top)


def _as_bytes(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise ValueError("expected a byte string")
    return value


def _pair_bytes(key: object, index: int) -> bytes:
    if not isinstance(key, (list, tuple)) or len(key) != _TOKEN_PAIR_LEN:
        raise ValueError("path config map key is not a [policy, name] pair")
    return _as_bytes(key[index])


def _hop_is_reverse(constr: object) -> bool:
    """A hop's orientation: ``Constr1`` reverses the leaf rate, ``Constr0`` keeps it."""
    if not isinstance(constr, cbor2.CBORTag):
        raise ValueError("hop orientation is not a Constr")
    return _constr_alt(constr.tag) == _HOP_REVERSE_ALT


def _hop_constr(is_reverse: bool) -> cbor2.CBORTag:
    return cbor2.CBORTag(
        _alt_constr_tag(_HOP_REVERSE_ALT if is_reverse else _CONSTR0),
        [],
    )


def _decode_paths(value: object) -> Tuple[Tuple[Tuple[int, bool], ...], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("path entry is not a list of paths")
    paths: list[tuple[tuple[int, bool], ...]] = []
    for path in value:
        if not isinstance(path, (list, tuple)):
            raise ValueError("derivation path is not a list of hops")
        hops: list[tuple[int, bool]] = []
        for hop in path:
            if not isinstance(hop, (list, tuple)) or len(hop) != _TOKEN_PAIR_LEN:
                raise ValueError("hop is not a (source_index, is_reverse) pair")
            if not isinstance(hop[0], int):
                raise ValueError("hop source index is not an integer")
            hops.append((int(hop[0]), _hop_is_reverse(hop[1])))
        paths.append(tuple(hops))
    return tuple(paths)
