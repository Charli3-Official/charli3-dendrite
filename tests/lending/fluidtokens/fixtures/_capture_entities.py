"""Dev-only: capture FluidTokens V3 inline datums from dbsync into a JSON fixture.

Fetches the inline datum CBOR (hex) of three currently-unspent mainnet UTxOs — one
pool, one loan, one request — and writes them to ``entities.json`` keyed by entity.
The captured CBOR is the source of truth for the byte-exact datum round-trip tests.

Run: PYTHONPATH=src python tests/lending/fluidtokens/fixtures/_capture_entities.py
Requires dbsync env (DBSYNC_*). Not run in CI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

# Mainnet target UTxOs (one per entity), overridable via env.
POOL_OUT_REF = os.environ.get(
    "FLUID_POOL_OUT_REF",
    "c6c34a7f46aa53a61e56af5fcd9a353407a544df0241472095e2908daaf11a6b#0",
)
LOAN_OUT_REF = os.environ.get(
    "FLUID_LOAN_OUT_REF",
    "c6c34a7f46aa53a61e56af5fcd9a353407a544df0241472095e2908daaf11a6b#1",
)
REQUEST_OUT_REF = os.environ.get(
    "FLUID_REQUEST_OUT_REF",
    "9c6baee9f46604e1b31c5af201fb4e9ebaaf453d1b63becc64097abf029679bb#0",
)

CONN = dict(
    host=os.environ.get("DBSYNC_HOST", "localhost"),
    port=int(os.environ.get("DBSYNC_PORT", "5432")),
    dbname=os.environ.get("DBSYNC_DB_NAME", "cexplorer"),
    user=os.environ.get("DBSYNC_USER", "cexplorer"),
    password=os.environ.get("DBSYNC_PASS", ""),
    connect_timeout=30,
)


def _inline_datum_hex(cur, out_ref: str) -> str:
    tx_hash, ix = out_ref.split("#")
    cur.execute(
        """select encode(d.bytes,'hex')
           from tx_out o
           join tx t on t.id = o.tx_id
           left join datum d on d.id = o.inline_datum_id
           where t.hash = decode(%s,'hex') and o.index = %s""",
        (tx_hash, int(ix)),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"no inline datum for out-ref {out_ref}")
    return row[0]


def capture() -> dict:
    targets = {
        "pool": POOL_OUT_REF,
        "loan": LOAN_OUT_REF,
        "request": REQUEST_OUT_REF,
    }
    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    out = {
        entity: dict(out_ref=out_ref, datum_cbor=_inline_datum_hex(cur, out_ref))
        for entity, out_ref in targets.items()
    }
    conn.close()
    return out


if __name__ == "__main__":
    here = Path(__file__).parent
    data = capture()
    (here / "entities.json").write_text(json.dumps(data, indent=2) + "\n")
    for entity, rec in data.items():
        print(f"{entity}: {rec['out_ref']} ({len(rec['datum_cbor'])} hex chars)")
