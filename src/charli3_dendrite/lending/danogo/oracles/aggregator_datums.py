"""Danogo oracle-aggregator datums (typed models).

The Float oracle-aggregator datum bundles the two logical structures shown in
Danogo's private Float-lending spec (`dano-finance/float-rate-lending-docs`):

* ``oracle_sources``: ``List<Source>`` — the leaf source registry. Each ``Source`` is
  a tagged union ``(OracleUtxoType, locator...)`` naming an on-chain UTxO whose datum
  yields one leaf rate, e.g. ``Orcfax(feed, validity, default_rate)``,
  ``LiqwidQToken(market_state_id, market_param_id)``, ``Charli3(...)``, etc.
* ``price_path``: ``List<Pair<Token, List<Path>>>`` where ``Path = List<(idx, bool)>``.
  Per collateral ``Token``, a list of alternative derivation paths; each path is a hop
  chain; each hop is ``(index into oracle_sources, inverted?)``. The alternative paths
  are computed independently, deviation-checked at 500 bps, then aggregated.

On mainnet (the "float-with-new-oracle" deployment) these are NOT stored as plain
Aiken ``Constr``s but hand-packed into byte strings the validator (de)serializes
itself, so they decode as ``List[bytes]`` rather than nested constructors.

Status of the collateral-price aggregator (as of this investigation):

* CONFIRMED (`redeemer.py`): the `OracleUtxoType` registry (17 variants) and the
  `OraclePriceCalcRdmr` byte-packing. Deviation tolerance is 500 bps everywhere.
* CONFIRMED: Danogo does NOT persist a computed price on-chain; the oracle-script UTxO
  is routing config (`Constr0[anchor, price_paths, oracle_sources, 500]`) and the leaf
  oracle UTxOs are reference inputs at transaction time.
* CONFIRMED (structural, this pass): ``price_paths`` entries reference
  ``oracle_sources`` BY INDEX — the max referenced index equals
  ``len(oracle_sources) - 1`` (50 with 51 sources). ``oracle_sources`` is a tagged
  union: a 1-byte tag with a tag-consistent
  fixed-length locator payload (e.g. the 56-byte locators are the two-id Liqwid
  qToken sources). See `test_aggregator_structure.py`.
* CONFIRMED (full grammar, this pass — `decode_price_path_entry`, validated bit-exact
  across every entry of all live config datums): each ``price_paths`` entry is
  ``token(28) calc_type(1) path+`` where ``path = n_hops(1) hop{n_hops}`` and
  ``hop = source_index(1) is_reverse(1) scale_exp(1)``. ``calc_type`` is
  ``CalcType`` (Normal/Splash); a token carries 1-3 alternative ``paths`` (the
  multi-path set that is averaged + deviation-checked); ``is_reverse`` is a clean
  bool; ``scale_exp`` is a per-hop power-of-ten that tracks the source kind. Hash-
  matching concrete redeemer leaves to sources confirms ``tag 1`` = Liqwid qToken
  (state+param pair) and ``tag 2`` = Danogo pool dtoken (payload = dtoken policy).
* VERIFIED (on-chain redeemer): the `OracleUtxoType` / `UTxOTarget` registries and the
  `OraclePriceCalcRdmr` layout are confirmed by decoding real `Withdraw(oracle_skh)`
  redeemers (see `redeemer.py::OraclePriceCalcRdmr` + the live test). Those redeemers
  also expose `prices: Pairs<TupleAsset, Pairs<TupleAsset, PRational>>` — the protocol's
  ground-truth collateral prices, which any datum-driven recompute must reproduce.
* VERIFIED (audit): Danogo's oracle-aggregator audit documents the algorithm — per
  source-type leaf parsing into accumulators (s2 Liqwid state, s3 Liqwid param, s9
  Minswap LP, ...), `calc_out_amount(in, num, denom, is_reverse)` hop math (is_reverse
  inverts num/denom), and multi-path averaging with the deviation check.
* PENDING: the full tag→source-kind table (tags 3,4,6,7,8,9,10) and each tag's exact
  locator field (which NFT/feed hash it stores). The hop grammar and `tag 1`/`tag 2`
  are settled; the remaining tags need more leaf↔source hash matching. End-to-end
  config-driven price reproduction (resolving sources to live UTxOs, walking hops,
  averaging the alternative paths with the 500 bps deviation check) builds on this
  decode but additionally needs that locator map. The aggregator repo itself is
  private, so all of this is pinned empirically against on-chain redeemer `prices`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List
from typing import NamedTuple

from pycardano import PlutusData
from pycardano import RawPlutusData

from charli3_dendrite.lending.danogo.oracles.redeemer import CalcType

_TOKEN_ID_LEN = 28
_HOP_LEN = 3
_MAX_HOPS = 8  # guard; longest real path seen is 4 hops


class OracleSource(NamedTuple):
    """One ``oracle_sources`` entry: a 1-byte ``tag`` + tag-consistent locator payload.

    The tag is the source-*kind* discriminator (not the on-chain ``OracleUtxoType``;
    one tag can expand to several leaves). Confirmed by matching concrete redeemer
    leaves against the config sources across many mainnet txs:

    * ``tag 1`` (56-byte payload) -- Liqwid qToken: ``market_state_policy`` ++
      ``market_param_policy`` (expands to the ``TLIQWID_MARKET_STATE`` +
      ``TLIQWID_MARKET_PARAM`` leaf pair). The first 28 bytes resolve to a plutusV2
      minting policy/script (the market-state NFT).
    * ``tag 2`` (28-byte payload) -- Danogo pool dtoken: payload = the pool address
      payment credential / dtoken policy (these also appear verbatim as ``price_paths``
      token ids).
    * ``tag 7`` (32-byte payload) -- Minswap V2 LP (``TMINSWAP_LP``): payload = the LP
      token *name* under the Minswap V2 LP policy
      ``f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c`` (matched 46/46 by
      asset name; hop ``scale_exp`` is always 3).

    Remaining tags (3,4,5,6,8,9,10) are external price-*feed* oracles (Liqwid-v2,
    Indigo, Djed, Charli3/Orcfax). Their locators are config-internal feed identifiers,
    NOT raw on-chain ids in the leaf UTxO (the feed leaves are also not always
    referenced as ``price_paths`` sources in the same tx), so pinning them needs the
    private aggregator source. Their byte layout (tag + fixed-length payload) is
    confirmed; ``tag 8`` (28-byte) carries a per-instance ``scale_exp`` (1/2/3) i.e. a
    multi-asset feed's decimals, and ``tag 10`` (2-byte) is a small index/enum.
    """

    tag: int
    payload: bytes


class PricePathHop(NamedTuple):
    """One hop in a derivation path.

    * ``source_index`` -- index into ``oracle_sources``.
    * ``is_reverse`` -- invert the leaf rate (swap num/denom), i.e. Danogo's
      ``calc_out_amount(.., is_reverse)``.
    * ``scale_exp`` -- per-hop power-of-ten scale. Confirmed to track the source kind
      (e.g. ``tag 7`` always 3, ``tag 2/3/4`` always 0) and to vary per-instance only
      for ``tag 8`` (1/2/3) -- i.e. the hop's decimals adjustment.
    """

    source_index: int
    is_reverse: bool
    scale_exp: int


class DecodedPricePath(NamedTuple):
    """A decoded ``price_paths`` entry: one collateral token's derivation paths."""

    token: str  # 28-byte collateral policy (hex)
    calc_type: CalcType
    paths: List[
        List[PricePathHop]
    ]  # 1-3 alternative paths, averaged + deviation-checked


def decode_oracle_source(entry: bytes) -> OracleSource:
    """Split a packed ``oracle_sources`` entry into ``(tag, payload)``."""
    if not entry:
        raise ValueError("empty oracle_source entry")
    return OracleSource(tag=entry[0], payload=entry[1:])


def decode_price_path_entry(entry: bytes, n_sources: int) -> DecodedPricePath:
    """Decode one packed ``price_paths`` entry.

    Layout (verified bit-exact across every entry of all live config datums)::

        entry = token(28) calc_type(1) path+
        path  = n_hops(1) hop{n_hops}
        hop   = source_index(1) is_reverse(1) scale_exp(1)

    ``n_sources`` bounds the source indices (every hop references a real source). Raises
    ValueError on a malformed entry.
    """
    if len(entry) < _TOKEN_ID_LEN + 1:
        raise ValueError("price_path entry shorter than token id + calc_type")
    token = entry[:_TOKEN_ID_LEN].hex()
    calc = CalcType(entry[_TOKEN_ID_LEN])
    i = _TOKEN_ID_LEN + 1
    paths: List[List[PricePathHop]] = []
    while i < len(entry):
        n_hops = entry[i]
        i += 1
        if not 1 <= n_hops <= _MAX_HOPS:
            raise ValueError(f"implausible hop count {n_hops}")
        hops: List[PricePathHop] = []
        for _ in range(n_hops):
            if i + _HOP_LEN > len(entry):
                raise ValueError("truncated hop")
            src, rev, scale = entry[i], entry[i + 1], entry[i + 2]
            i += _HOP_LEN
            if src > n_sources - 1:
                raise ValueError(f"source index {src} out of range")
            if rev not in (0, 1):
                raise ValueError(f"is_reverse {rev} is not a bool")
            hops.append(
                PricePathHop(source_index=src, is_reverse=bool(rev), scale_exp=scale),
            )
        paths.append(hops)
    if not paths:
        raise ValueError("price_path entry has no derivation paths")
    return DecodedPricePath(token=token, calc_type=calc, paths=paths)


@dataclass
class OraclePathDatum(PlutusData):
    """Global Float oracle config (``Constr0`` of four positional fields).

    Layout: ``[anchor, price_paths, oracle_sources, deviation_bps]``.

    Confirmed mainnet shape — four positional fields:

    * ``anchor``: 28-byte script hash anchoring the config.
    * ``price_paths``: packed ``price_path`` entries (per-token derivation paths). Each
      entry is ``<28-byte token id> 00 <hops>`` where hops reference ``oracle_sources``
      by index (confirmed: indices stay within ``0..len(oracle_sources)-1``).
    * ``oracle_sources``: packed source specs, each ``<1-byte tag> <locator hash(es)>``
      with a tag-consistent payload length.
    * ``deviation_bps``: aggregation deviation tolerance (observed 500).

    The two packed lists are surfaced as ``List[bytes]``; the per-entry hop / locator
    sub-structure decode is verified incrementally against on-chain redeemer prices.
    """

    CONSTR_ID = 0
    anchor: bytes
    price_paths: List[bytes]
    oracle_sources: List[bytes]
    deviation_bps: int

    def decode_sources(self) -> List[OracleSource]:
        """Decode ``oracle_sources`` into ``(tag, payload)`` entries."""
        return [decode_oracle_source(s) for s in self.oracle_sources]

    def decode_paths(self) -> List[DecodedPricePath]:
        """Decode every ``price_paths`` entry into typed derivation paths."""
        n = len(self.oracle_sources)
        return [decode_price_path_entry(p, n) for p in self.price_paths]

    def paths_by_token(self) -> dict[str, DecodedPricePath]:
        """``{collateral_policy_hex: DecodedPricePath}`` for this config's tokens."""
        return {d.token: d for d in self.decode_paths()}


@dataclass
class OracleGlobalConfig:
    """Global oracle source registry — a packed byte blob.

    Unlike the position datums this is NOT a `Constr`: the datum's top-level CBOR is
    a single byte string the validator unpacks manually (a source-type table plus
    per-source locators). `raw` holds those packed bytes; the structured decode is
    deferred. This is a plain dataclass (not `PlutusData`) precisely because the
    datum has no `Constr` wrapper.
    """

    raw: bytes

    @classmethod
    def from_cbor(cls, data: str | bytes) -> OracleGlobalConfig:
        """Read the packed byte body from a datum whose top-level CBOR is bytes."""
        body = RawPlutusData.from_cbor(data).data
        if not isinstance(body, bytes):
            raise ValueError("OracleGlobalConfig datum is not a packed byte string")
        return cls(raw=body)
