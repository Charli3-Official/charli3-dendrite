"""Danogo `OraclePriceCalcRdmr` withdrawal redeemer (verified against mainnet).

The Float lending validator verifies collateral prices via a withdraw-zero redeemer
at ``oracle_skh``. Two things are now confirmed by decoding real mainnet redeemers
(reward redeemers at the oracle stake script; see `tests/.../test_live.py`):

* The on-chain redeemer is ``Constr0`` of five fields::

      oracle_source_idx : Int
      oracle_path_idxs  : List<Int>
      oracle_idxs       : List<(UTxOTarget, OracleUtxoType, Int)>   # CBOR Constr tuples
      prices            : Pairs<TupleAsset, Pairs<TupleAsset, PRational>>
      borrowRates       : Pairs<YieldToken, Basis>

  ``OraclePriceCalcRdmr.from_cbor`` parses this. Every ``OracleUtxoType`` /
  ``UTxOTarget`` value seen on-chain decodes to a known enum member, confirming the
  registry below. ``prices`` is the ground truth: quote-asset -> collateral-asset ->
  exact ``PRational`` rate (e.g. ADA -> USDM = 16271/100000).

* IMPORTANT — the ``oracle_idxs`` field is structured CBOR Constr tuples on-chain,
  NOT the packed 3-byte ByteArray shown in Danogo's "Create a Flexible Loan
  Transaction" integration guide (``010800020d01``). The packed form is kept below as
  ``decode_oracle_idxs`` / ``encode_oracle_idxs`` because it matches that documented
  example, but it is an off-chain notation, not the on-chain redeemer encoding.

The `OraclePathDatum` config (see `aggregator_datums.py`) packs the source registry +
price paths; the aggregator algorithm (per-source leaf parsing + ``calc_out_amount``
hop math where ``is_reverse`` inverts num/denom, multi-path averaging + deviation
check) is documented in Danogo's oracle-aggregator audit report.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import cbor2  # type: ignore[import-not-found]

from charli3_dendrite.lending.danogo.datums import asset_unit
from charli3_dendrite.lending.units import constr_alt


class UTxOTarget(IntEnum):
    """Where an oracle price UTxO is found in the spending transaction."""

    REF = 0  # reference input
    OUT = 1  # transaction output
    IN = 2  # spent input


class OracleUtxoType(IntEnum):
    """Leaf oracle source types, encoded by their enum index (0..16).

    This is the parser registry: each value names an external (or Danogo-owned)
    on-chain UTxO whose datum the validator reads to obtain one leaf price.
    """

    TORCFAX_FSP = 0
    TORCFAX_FS = 1
    TLIQWID_MARKET_STATE = 2
    TLIQWID_MARKET_PARAM = 3
    TLIQWID_ORACLE_V2 = 4
    TDANOGO_POOL = 5
    TINDIGO = 6
    TDJED = 7
    TDANOGO_STAKING = 8
    TMINSWAP_LP = 9
    TLIQWID_ORACLE_V1 = 10
    TSPLASH_CPAMM_G1 = 11
    TSPLASH_CPAMM_G2 = 12
    TSPLASH_CPAMM_G3 = 13
    TSPLASH_STABLE = 14
    TCHARLI3 = 15
    TMINSWAP_LP_STABLE = 16


class CalcType(IntEnum):
    """Price calculation mode in `OraclePriceInfo` (0=Normal, 1=Splash)."""

    NORMAL = 0
    SPLASH = 1


# One oracle spec: (where to find the UTxO, how to parse it, its index in that list).
OracleIndex = tuple[UTxOTarget, OracleUtxoType, int]

_BYTES_PER_ORACLE = 3
_MAX_BYTE = 0xFF


def decode_oracle_idxs(data: bytes | str) -> list[OracleIndex]:
    """Decode the redeemer ``oracle_idxs`` ByteArray (3 bytes per oracle).

    Each 3-byte chunk is (UTxOTarget, OracleUtxoType, index). Raises ValueError if
    the length is not a multiple of three or a byte is out of enum range.
    """
    raw = bytes.fromhex(data) if isinstance(data, str) else data
    if len(raw) % _BYTES_PER_ORACLE:
        raise ValueError("oracle_idxs length must be a multiple of 3")
    out: list[OracleIndex] = []
    for i in range(0, len(raw), _BYTES_PER_ORACLE):
        target, otype, index = raw[i], raw[i + 1], raw[i + 2]
        out.append((UTxOTarget(target), OracleUtxoType(otype), index))
    return out


def encode_oracle_idxs(specs: list[OracleIndex]) -> bytes:
    """Encode oracle specs back into the ``oracle_idxs`` ByteArray."""
    raw = bytearray()
    for target, otype, index in specs:
        if not 0 <= index <= _MAX_BYTE:
            raise ValueError("oracle index must fit in one byte")
        raw += bytes((int(target), int(otype), index))
    return bytes(raw)


def decode_oracle_path_idxs(data: bytes | str) -> list[int]:
    """Decode ``oracle_path_idxs`` (one byte per OraclePath reference-input index)."""
    raw = bytes.fromhex(data) if isinstance(data, str) else data
    return list(raw)


# CBOR constructor tags: alts 0..6 -> 121..127; alts 7+ -> 1280 + (alt - 7).
_CONSTR_TAG_MIN = 121
_CONSTR_TAG_MAX = 127
_CONSTR_TAG_EXT = 1280
_CONSTR_EXT_OFFSET = 7
_RDMR_BORROW_FIELD = 4

# Thin alias over the shared primitive; kept for existing danogo call sites.
_constr_alt = constr_alt


def _alt_constr_tag(alt: int) -> int:
    """Inverse of `_constr_alt`: alternative index -> CBOR constructor tag."""
    if alt <= _CONSTR_TAG_MAX - _CONSTR_TAG_MIN:
        return _CONSTR_TAG_MIN + alt
    return _CONSTR_TAG_EXT + alt - _CONSTR_EXT_OFFSET


# On-chain encoding rules (verified against mainnet create-loan redeemers): arrays are
# indefinite-length (0x9f..0xff), maps are definite, nullary constrs are an empty
# definite array (tag + 0x80), and constrs with fields wrap an indefinite array.
_TAG121 = b"\xd8\x79"  # CBOR tag 121 (Constr alt 0) prefix
_INDEF_START = b"\x9f"
_BREAK = b"\xff"
_MAP_SHORT_MAX = 23
_MAP_BYTE_MAX = 255
_MAP_WORD_MAX = 65535


def _indef(items: list[bytes]) -> bytes:
    """Encode a list of pre-encoded items as an indefinite-length CBOR array."""
    return _INDEF_START + b"".join(items) + _BREAK


def _map_header(n: int) -> bytes:
    """Definite-length CBOR map header for n entries."""
    if n <= _MAP_SHORT_MAX:
        return bytes([0xA0 | n])
    if n <= _MAP_BYTE_MAX:
        return bytes([0xB8, n])
    if n <= _MAP_WORD_MAX:
        return b"\xb9" + n.to_bytes(2, "big")
    raise ValueError("map too large to encode")


def _defmap(pairs: list[tuple[bytes, bytes]]) -> bytes:
    """Encode pre-encoded (key, value) byte pairs as a definite-length CBOR map."""
    return _map_header(len(pairs)) + b"".join(k + v for k, v in pairs)


def _enc_tuple_asset(unit: str) -> bytes:
    """Dendrite unit -> TupleAsset as an indefinite array [policy, name]."""
    if unit == "lovelace":
        policy, name = b"", b""
    else:
        policy, name = bytes.fromhex(unit[:56]), bytes.fromhex(unit[56:])
    return _indef([cbor2.dumps(policy), cbor2.dumps(name)])


def _enc_prational(num: int, denom: int) -> bytes:
    """PRational as Constr0 wrapping an indefinite array [num, denom]."""
    return _TAG121 + _indef([cbor2.dumps(num), cbor2.dumps(denom)])


def _enc_nullary(tag: int) -> bytes:
    """A nullary constructor (empty definite array): tag + 0x80."""
    return cbor2.dumps(cbor2.CBORTag(tag, []))


def _tuple_asset_unit(arr: object) -> str:
    """Dendrite unit string from a ``TupleAsset`` CBOR value (``[policy, name]``)."""
    seq = list(arr) if isinstance(arr, (list, tuple)) else []
    policy = seq[0] if len(seq) > 0 and isinstance(seq[0], bytes) else b""
    name = seq[1] if len(seq) > 1 and isinstance(seq[1], bytes) else b""
    return asset_unit(policy, name)


@dataclass
class OraclePriceCalcRdmr:
    """The on-chain Float oracle withdrawal redeemer (verified against mainnet CBOR).

    ``prices`` is flattened to ``quote_unit -> {collateral_unit: (num, denom)}`` and is
    the protocol's ground-truth collateral pricing for the spending transaction.
    """

    oracle_source_idx: int
    oracle_path_idxs: list[int]
    oracle_idxs: list[OracleIndex]
    prices: dict[str, dict[str, tuple[int, int]]]
    borrow_rates: dict[str, int]

    @classmethod
    def from_cbor(cls, data: str | bytes) -> OraclePriceCalcRdmr:
        """Parse the redeemer CBOR (a `Constr0` of five fields)."""
        raw = bytes.fromhex(data) if isinstance(data, str) else data
        top = cbor2.loads(raw)
        fields = top.value
        src, paths, idxs, prices = fields[0], fields[1], fields[2], fields[3]
        borrow = fields[_RDMR_BORROW_FIELD] if len(fields) > _RDMR_BORROW_FIELD else {}

        oracle_idxs: list[OracleIndex] = []
        for entry in idxs:
            # Each entry is a bare 3-element array [Constr(target), Constr(otype), idx].
            tv = entry.value if hasattr(entry, "value") else entry
            target = UTxOTarget(_constr_alt(tv[0].tag))
            otype = OracleUtxoType(_constr_alt(tv[1].tag))
            oracle_idxs.append((target, otype, int(tv[2])))

        out_prices: dict[str, dict[str, tuple[int, int]]] = {}
        for quote, inner in dict(prices).items():
            qunit = _tuple_asset_unit(quote)
            collat_prices: dict[str, tuple[int, int]] = {}
            for collat, prat in dict(inner).items():
                pv = prat.value
                collat_prices[_tuple_asset_unit(collat)] = (int(pv[0]), int(pv[1]))
            out_prices[qunit] = collat_prices

        borrow_rates: dict[str, int] = {}
        if isinstance(borrow, dict):
            for token, basis in borrow.items():
                try:
                    borrow_rates[_tuple_asset_unit(token)] = int(basis)
                except (TypeError, ValueError):
                    continue

        return cls(
            oracle_source_idx=int(src),
            oracle_path_idxs=[int(x) for x in paths],
            oracle_idxs=oracle_idxs,
            prices=out_prices,
            borrow_rates=borrow_rates,
        )

    def to_cbor(self) -> bytes:
        """Re-emit the exact on-chain redeemer CBOR (round-trips `from_cbor`)."""
        idxs = _indef(
            [
                _indef(
                    [
                        _enc_nullary(_alt_constr_tag(int(target))),
                        _enc_nullary(_alt_constr_tag(int(otype))),
                        cbor2.dumps(index),
                    ],
                )
                for target, otype, index in self.oracle_idxs
            ],
        )
        paths = _indef([cbor2.dumps(p) for p in self.oracle_path_idxs])
        prices = _defmap(
            [
                (
                    _enc_tuple_asset(quote),
                    _defmap(
                        [
                            (_enc_tuple_asset(collat), _enc_prational(num, denom))
                            for collat, (num, denom) in inner.items()
                        ],
                    ),
                )
                for quote, inner in self.prices.items()
            ],
        )
        borrow = _defmap(
            [
                (_enc_tuple_asset(token), cbor2.dumps(basis))
                for token, basis in self.borrow_rates.items()
            ],
        )
        return _TAG121 + _indef(
            [cbor2.dumps(self.oracle_source_idx), paths, idxs, prices, borrow],
        )
