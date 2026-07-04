"""Dev-only: capture FluidTokens V3 entities from dbsync into a JSON fixture.

Fetches, for three currently-unspent mainnet UTxOs — one pool, one loan, one
request — the inline datum CBOR (hex), the holding address, and the UTxO value
(ADA + multi-asset), writing them to ``entities.json`` keyed by entity. Each entry
is ``{out_ref, address, datum_cbor, assets}`` where ``assets`` is the charli3
``Assets`` shape: ``{"lovelace": <int>, "<policyhex><namehex>": <int>, ...}``. The
captured CBOR is the source of truth for the byte-exact datum round-trip tests, and
the address/assets feed read-only state construction.

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


def _address_and_assets(cur, out_ref: str) -> tuple[str, dict]:
    """(holding address, charli3 ``Assets`` dict) of an unspent UTxO.

    ``assets`` always carries ``lovelace`` plus one ``<policyhex><namehex>`` entry per
    multi-asset held at the output.
    """
    tx_hash, ix = out_ref.split("#")
    cur.execute(
        """select a.address, o.value
           from tx_out o
           join tx t on t.id = o.tx_id
           join address a on a.id = o.address_id
           where t.hash = decode(%s,'hex') and o.index = %s""",
        (tx_hash, int(ix)),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"no tx_out for out-ref {out_ref}")
    address, lovelace = row[0], int(row[1])
    assets: dict[str, int] = {"lovelace": lovelace}
    cur.execute(
        """select encode(ma.policy,'hex'), encode(ma.name,'hex'), mto.quantity
           from tx_out o
           join tx t on t.id = o.tx_id
           join ma_tx_out mto on mto.tx_out_id = o.id
           join multi_asset ma on ma.id = mto.ident
           where t.hash = decode(%s,'hex') and o.index = %s""",
        (tx_hash, int(ix)),
    )
    for policy_hex, name_hex, quantity in cur.fetchall():
        assets[f"{policy_hex}{name_hex}"] = int(quantity)
    return address, assets


def capture() -> dict:
    targets = {
        "pool": POOL_OUT_REF,
        "loan": LOAN_OUT_REF,
        "request": REQUEST_OUT_REF,
    }
    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    out = {}
    for entity, out_ref in targets.items():
        address, assets = _address_and_assets(cur, out_ref)
        out[entity] = dict(
            out_ref=out_ref,
            address=address,
            datum_cbor=_inline_datum_hex(cur, out_ref),
            assets=assets,
        )
    conn.close()
    return out


if __name__ == "__main__":
    here = Path(__file__).parent
    data = capture()
    (here / "entities.json").write_text(json.dumps(data, indent=2) + "\n")
    for entity, rec in data.items():
        n_assets = len(rec["assets"])
        print(
            f"{entity}: {rec['out_ref']} "
            f"({len(rec['datum_cbor'])} hex chars, {n_assets} assets)",
        )
