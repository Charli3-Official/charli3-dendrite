"""Mine an observation-based oracle recipe registry from db-sync.

Walks the most-recent reward redeemers at the Danogo oracle script (each carrying an
``OraclePriceCalcRdmr``), and for every ``collateral|quote`` price it names, learns the
ordered source-leaf ``Recipe`` that reproduces that price (reusing the per-tx capture
logic in ``_capture_forward_pricing``). Newest-first iteration means the most-recent tx
that priced a pair wins. The frozen ``{collateral|quote: Recipe}`` registry is written to
``oracle_recipes.json`` for live replay later.

Run manually (needs db-sync; the DBSYNC_* env must be exported, since ``cap.CONN`` is
built from it at import time)::

    DBSYNC_*... PYTHONPATH=src python tests/lending/danogo/fixtures/_capture_recipes.py [limit]

``limit`` (default 500) caps how many recent reward redeemers are scanned. Each tx fires
several small ref-input queries, so a large limit may take a couple of minutes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _capture_forward_pricing as cap  # noqa: E402

import psycopg  # noqa: E402

from charli3_dendrite.lending.danogo.oracles.locator import (  # noqa: E402
    recipe_from_observation,
    registry_to_dict,
)
from charli3_dendrite.lending.danogo.oracles.redeemer import (  # noqa: E402
    OraclePriceCalcRdmr,
)

OUT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "charli3_dendrite"
    / "lending"
    / "danogo"
    / "oracles"
    / "recipes.json"
)

_RECENT_REWARD_TXS = """
    SELECT encode(tx.hash,'hex') FROM redeemer r JOIN tx ON tx.id=r.tx_id
    WHERE r.purpose='reward' AND r.script_hash=decode(%s,'hex')
    ORDER BY r.id DESC LIMIT %s
"""


def mine(limit: int) -> dict:
    """Learn the newest recipe per ``collateral|quote`` from recent oracle txs."""
    conn = psycopg.connect(**cap.CONN)
    cur = conn.cursor()
    cur.execute(_RECENT_REWARD_TXS, (cap.ORACLE_SKH, limit))
    tx_hashes = [row[0] for row in cur.fetchall()]

    registry: dict = {}
    pairs_seen = 0
    pairs_solved = 0
    for txh in tx_hashes:
        rdmr_cbor = cap._reward_redeemer_cbor(cur, txh)
        if not rdmr_cbor:
            continue
        try:
            rdmr = OraclePriceCalcRdmr.from_cbor(rdmr_cbor)
        except Exception:
            continue
        refs = cap._ref_inputs(cur, txh)
        for quote, inner in rdmr.prices.items():
            for collat, (num, denom) in inner.items():
                if (num, denom) == (1, 1):
                    continue  # identity peg: no leaf to align
                key = f"{collat}|{quote}"
                if key in registry:
                    continue  # newest-first: most-recent tx already won this pair
                pairs_seen += 1
                case = cap._case(rdmr, refs, quote, collat, num, denom)
                if case is not None:
                    registry[key] = recipe_from_observation(case["leaves"])
                    pairs_solved += 1

    conn.close()
    print(f"scanned {len(tx_hashes)} txs; {pairs_solved}/{pairs_seen} new pairs solved")
    return registry


def main(argv: list[str]) -> None:
    limit = int(argv[0]) if argv else 500
    registry = mine(limit)
    OUT.write_text(
        json.dumps(registry_to_dict(registry), indent=2, sort_keys=True) + "\n"
    )
    keys = sorted(registry)
    print(f"wrote {len(registry)} recipes to {OUT}")
    for key in keys[:10]:
        print(f"  {key}")


if __name__ == "__main__":
    main(sys.argv[1:])
