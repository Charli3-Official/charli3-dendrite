"""Capture forward-pricing fixtures from real Danogo oracle redeemers.

For each chosen tx this parses the ``OraclePriceCalcRdmr`` (the withdraw-zero redeemer
at the oracle script), resolves the transaction's reference inputs from db-sync, picks
the price-source leaves the redeemer names in ``oracle_idxs``, and emits, per priced
collateral, the ordered leaves whose per-leaf forward rates reproduce the on-chain
price. Output: ``tests/lending/danogo/fixtures/forward_pricing.json``. Run manually
(needs db-sync)::

    DBSYNC_*... python tests/lending/danogo/fixtures/_capture_forward_pricing.py [txprefix...]

Empirical alignment findings (db-sync row layout + ordering), pinned against on-chain
redeemer ``prices`` and confirmed by leaf-datum identity:

* db-sync ``reference_tx_in`` join: in this db-sync ``reference_tx_in.tx_out_id`` holds
  the *producing transaction's* ``tx.id`` (not ``tx_out.id``), so reference inputs are
  resolved with ``tx_out.tx_id = reference_tx_in.tx_out_id AND tx_out.index =
  reference_tx_in.tx_out_index``.
* REF ordering (CONFIRMED): a ``UTxOTarget.REF`` ``oracle_idxs`` entry's index selects
  a reference input from the list sorted canonically by ``(prev_tx_hash,
  prev_output_index)`` (Plutus script-context order). Verified by matching the redeemer
  leaf type at each index to the resolved leaf datum (e.g. a ``TLIQWID_ORACLE_V2`` entry
  at index 3 lands on the Liqwid-oracle datum at canonical index 3).
* Leaf-to-price composition (CONFIRMED): the named REF leaves, parsed to forward rates
  by the verified per-source parsers (``oracles.reproduce``), compose to each on-chain
  price as a product of a subset of leaf rates (each forward or inverted) times one
  power-of-ten. Because the product is commutative the captured leaf order is the
  canonical ``oracle_idxs`` REF order; ``is_reverse`` is the per-leaf inversion and the
  power-of-ten is carried on the first captured leaf's ``scale_exp``.
* NOT used here (and why): the live ``OraclePathDatum`` price-path config is *not*
  keyed by the redeemer's collateral/quote asset unit (no historical tx prices a token
  whose policy is a config ``price_paths`` key), and mapping a path hop's
  ``source_index`` to a concrete leaf still needs the oracle-sources locator map (the
  aggregator source is private; see ``oracles.aggregator_datums``). So leaves are
  aligned to prices by reproducing the price from the resolved leaves, not by walking a
  collateral-keyed config path. The per-tx path datum is reachable as the reference
  input at ``oracle_path_idxs`` should a later pass complete that locator map.
"""
from __future__ import annotations

import json
import os
import sys
from fractions import Fraction
from itertools import combinations
from itertools import product as iproduct
from pathlib import Path

import psycopg

from charli3_dendrite.lending.danogo.oracles.forward import leaf_discriminator
from charli3_dendrite.lending.danogo.oracles.leaves import parse_minswap_lp
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.oracles.reproduce import leaf_forward_rates
from charli3_dendrite.lending.danogo.oracles.reproduce import splash_lp_candidate

# Oracle withdraw script hash (reward redeemer carrying the OraclePriceCalcRdmr).
ORACLE_SKH = "012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a"
_MAX_SUBSET = 4  # longest real derivation path is 4 hops
_MAX_SCALE_EXP = 12

CONN = dict(
    host=os.environ["DBSYNC_HOST"],
    port=int(os.environ.get("DBSYNC_PORT", 5432)),
    dbname=os.environ["DBSYNC_DB_NAME"],
    user=os.environ["DBSYNC_USER"],
    password=os.environ["DBSYNC_PASS"],
    connect_timeout=30,
)
OUT = Path(__file__).parent / "forward_pricing.json"


def _full_hash(cur, prefix: str) -> str | None:
    """Resolve a tx-hash prefix via an indexed range scan on the bytea hash."""
    raw = bytes.fromhex(prefix)
    lo = raw + b"\x00" * (32 - len(raw))
    hi = raw + b"\xff" * (32 - len(raw))
    cur.execute(
        "SELECT encode(hash,'hex') FROM tx WHERE hash BETWEEN %s AND %s LIMIT 1",
        (lo, hi),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _reward_redeemer_cbor(cur, txh: str) -> str | None:
    cur.execute(
        """SELECT encode(rd.bytes,'hex') FROM redeemer r
           JOIN redeemer_data rd ON rd.id=r.redeemer_data_id
           JOIN tx ON tx.id=r.tx_id
           WHERE r.purpose='reward' AND r.script_hash=decode(%s,'hex')
             AND tx.hash=decode(%s,'hex')""",
        (ORACLE_SKH, txh),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _ref_inputs(cur, txh: str) -> list[dict]:
    """Reference inputs of ``txh`` sorted canonically by (prev_tx_hash, prev_idx)."""
    cur.execute(
        """SELECT encode(ptx.hash,'hex') AS pth, pto.index AS pidx, a.address,
                  encode(d.bytes,'hex') AS datum, pto.value AS lovelace
           FROM reference_tx_in rti
           JOIN tx_out pto ON pto.tx_id = rti.tx_out_id AND pto.index = rti.tx_out_index
           JOIN tx ptx ON ptx.id = pto.tx_id
           JOIN address a ON a.id = pto.address_id
           LEFT JOIN datum d ON d.id = pto.inline_datum_id
           JOIN tx ON tx.id = rti.tx_in_id
           WHERE tx.hash = decode(%s,'hex')""",
        (txh,),
    )
    rows = [
        {
            "prev_tx": pth,
            "prev_idx": int(pidx),
            "address": addr,
            "datum": datum,
            "lovelace": int(lov),
            "assets": [],
        }
        for (pth, pidx, addr, datum, lov) in cur.fetchall()
    ]
    for r in rows:
        cur.execute(
            """SELECT encode(ma.policy,'hex'), encode(ma.name,'hex'), mto.quantity
               FROM ma_tx_out mto JOIN multi_asset ma ON ma.id=mto.ident
               JOIN tx_out o ON o.id=mto.tx_out_id JOIN tx t ON t.id=o.tx_id
               WHERE t.hash=decode(%s,'hex') AND o.index=%s
               ORDER BY ma.policy, ma.name""",
            (r["prev_tx"], r["prev_idx"]),
        )
        r["assets"] = [[p, n, int(q)] for (p, n, q) in cur.fetchall()]
    rows.sort(key=lambda r: (r["prev_tx"], r["prev_idx"]))
    return rows


def _identifying_nft(assets) -> tuple[str, str]:
    for p, n, q in assets:
        if int(q) == 1:
            return p, n
    return ("", "")


def _handle_identity(otype, ref) -> tuple[str, str, str]:
    """(nft_policy, nft_name, datum_key) used to re-find this leaf's UTxO live.

    The server-side asset filter is the (policy, name) returned here; the datum_key
    narrows the result when that filter is not unique. Minswap pools all carry one
    shared MSP token (matching every pool, far past the query cap), so they are filtered
    by their distinctive non-ADA reserve token instead and confirmed by the asset pair.
    Other kinds use their first qty-1 token; Liqwid feeds add a datum feed-id key.
    """
    datum = ref["datum"]
    dkey = leaf_discriminator(otype.name, datum)
    if otype == OracleUtxoType.TMINSWAP_LP and datum:
        asset_a, _, asset_b, _ = parse_minswap_lp(datum)
        distinctive = asset_a if asset_a != "lovelace" else asset_b
        if distinctive and distinctive != "lovelace":
            return distinctive[:56], distinctive[56:], dkey
    pol, name = _identifying_nft(ref["assets"])
    return pol, name, dkey


def _leaf_rate(otype_name: str, datum: str | None, assets) -> Fraction | None:
    """Forward leaf rate, mirroring the alignment test's ``leaf_rate`` contract.

    Quote is fixed to ``lovelace`` so the captured cases match how the test walks them.
    """
    leaf = OracleLeaf(otype=OracleUtxoType[otype_name], datum=datum, assets=assets)
    forward = leaf_forward_rates(leaf)
    if forward:
        return forward[0][1]
    return splash_lp_candidate(leaf, "lovelace")


def _pow10(ratio: Fraction) -> int | None:
    for k in range(-_MAX_SCALE_EXP, _MAX_SCALE_EXP + 1):
        if ratio == Fraction(10) ** k:
            return k
    return None


def _solve(rates: list[Fraction], want: Fraction):
    """Smallest ordered subset (with per-leaf invert + one power-of-ten) == want.

    Returns ``(indices, reverses, scale_exp)`` for the subset, or None.
    """
    idxs = list(range(len(rates)))
    for size in range(1, min(_MAX_SUBSET, len(rates)) + 1):
        for combo in combinations(idxs, size):
            for signs in iproduct((False, True), repeat=size):
                prod = Fraction(1)
                for i, rev in zip(combo, signs):
                    prod *= (1 / rates[i]) if rev else rates[i]
                if prod == 0:
                    continue
                exp = _pow10(want / prod)
                if exp is not None:
                    return list(combo), list(signs), exp
    return None


def _case(
    rdmr: OraclePriceCalcRdmr,
    refs: list[dict],
    quote: str,
    collat: str,
    num: int,
    denom: int,
) -> dict | None:
    """Build one fixture case if the named REF leaves reproduce the price."""
    ref_leaves = [
        (otype, refs[i])
        for (target, otype, i) in rdmr.oracle_idxs
        if target == UTxOTarget.REF and i < len(refs)
    ]
    # Keep only leaves that parse to a forward rate; an unparseable leaf (e.g. a Liqwid
    # market-param leaf, which is read jointly with its state leaf) is simply not a
    # standalone hop and is excluded from the composition search.
    parseable: list[tuple[OracleUtxoType, dict, Fraction]] = []
    for otype, ref in ref_leaves:
        assets = tuple((p, n, int(q)) for p, n, q in ref["assets"]) + (
            ("", "", ref["lovelace"]),
        )
        rate = _leaf_rate(otype.name, ref["datum"], assets)
        if rate is not None and rate > 0:
            parseable.append((otype, ref, rate))
    if not parseable:
        return None
    rates = [rate for (_, _, rate) in parseable]
    solved = _solve(rates, Fraction(num, denom))
    if solved is None:
        return None
    indices, reverses, scale_exp = solved
    leaves = []
    for hop_pos, (idx, rev) in enumerate(zip(indices, reverses)):
        otype, ref, _ = parseable[idx]
        pol, name, dkey = _handle_identity(otype, ref)
        leaves.append(
            {
                "source_index": idx,
                "otype": otype.name,
                "is_reverse": rev,
                "scale_exp": scale_exp if hop_pos == 0 else 0,
                "address": ref["address"],
                "nft_policy": pol,
                "nft_name": name,
                "datum_key": dkey,
                "datum": ref["datum"],
                "assets": ref["assets"] + [["", "", ref["lovelace"]]],
            }
        )
    return {
        "tx": None,
        "token": collat,
        "quote": quote,
        "expected": [num, denom],
        "leaves": leaves,
    }


def main(prefixes: list[str]) -> None:
    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    out: list[dict] = []
    for pref in prefixes:
        txh = _full_hash(cur, pref)
        if not txh:
            continue
        rdmr_cbor = _reward_redeemer_cbor(cur, txh)
        if not rdmr_cbor:
            continue
        rdmr = OraclePriceCalcRdmr.from_cbor(rdmr_cbor)
        refs = _ref_inputs(cur, txh)
        for quote, inner in rdmr.prices.items():
            for collat, (num, denom) in inner.items():
                if (num, denom) == (1, 1):
                    continue  # identity peg: no leaf to align
                case = _case(rdmr, refs, quote, collat, num, denom)
                if case is not None:
                    case["tx"] = txh
                    out.append(case)
    OUT.write_text(json.dumps(out, indent=2))
    print(f"wrote {len(out)} forward-pricing cases to {OUT}")
    conn.close()


# Curated historical Danogo oracle txs: multi-leaf collateral prices (cc74a9, 002c5250)
# plus single-leaf dtoken prices, chosen so the captured set exercises 1- and multi-hop
# leaf compositions.
_DEFAULT_PREFIXES = [
    "cc74a9bd6b98",
    "002c5250a4d1",
    "0de94a288e8f",
    "51733a3ae4a3",
    "4cf065ee36cd",
    "518a54a42813",
]

if __name__ == "__main__":
    main(sys.argv[1:] or _DEFAULT_PREFIXES)
