"""Dev-only: capture FluidTokens V4 mainnet UTxOs from dbsync into a JSON fixture.

Writes ``entities.json`` next to this script. Every entry is ``{out_ref, address,
datum_cbor, assets}`` where ``assets`` is the charli3 ``Assets`` shape
(``{"lovelace": <int>, "<policyhex><namehex>": <int>, ...}``). Keys:

- ``config``: every UTxO that has held the protocol config NFT, oldest first (spent
  versions included, so the config history is replayable).
- ``lender_manager_config``: the current lender-manager config UTxO.
- ``pool``, ``pool_manager``, ``loan``, ``request``, ``asset_manager``,
  ``lender_manager``, ``locked_borrower_manager``: every unspent UTxO at that entity's
  spend-script payment credential, ordered by out-ref.

The captured CBOR is the source of truth for the byte-exact datum tests.

Run: PYTHONPATH=src python tests/lending/fluidtokens_v4/fixtures/_capture_entities.py
Requires dbsync env (DBSYNC_*). Not run in CI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

CONFIG_NFT = (
    "235b32040fe1177c03b1d34febc470440c6eaaa2228a9c1b0e375200",
    "706172616d6574657273",
)
LENDER_MANAGER_CONFIG_NFT = (
    "fb6ae2027358b4a0b62710eb95102d87fa13f66ecf55d8943699c492",
    "706172616d6574657273",
)
ENTITY_CREDENTIALS = {
    "pool": "ecd0c0fc554beb43ade65d45dd471ede4d9cfc9b22bb70de8e113458",
    "pool_manager": "dfb1d21e529af28d82004eee5689e3304bb896c35ce8fe65c85612b0",
    "loan": "b5763e3c9cb7f1a2167f74f76cb635d24d138a4cc5cf9d87659c662d",
    "request": "390d27f97868840864922fb0b5657ae4982b5be8bf03bf9cec81125e",
    "asset_manager": "b256da41be022ba1a28fe92b32eeea00109310e3f0aea1cb7791d98f",
    "lender_manager": "6743f4b69446b4e066cfb89daa3d01ef041b6d2646968993be4cec09",
    "locked_borrower_manager": (
        "072eee302404c8d869bbc61a2c45502d371d96ec7d6aae7b179bfaa3"
    ),
}

CONN = dict(
    host=os.environ.get("DBSYNC_HOST", "localhost"),
    port=int(os.environ.get("DBSYNC_PORT", "5432")),
    dbname=os.environ.get("DBSYNC_DB_NAME", "cexplorer"),
    user=os.environ.get("DBSYNC_USER", "cexplorer"),
    password=os.environ.get("DBSYNC_PASS", ""),
    connect_timeout=30,
)

_RECORD_SQL = """
    select encode(t.hash, 'hex'), o.index, a.address, o.value,
           encode(d.bytes, 'hex'), o.id
    from tx_out o
    join tx t on t.id = o.tx_id
    join address a on a.id = o.address_id
    left join datum d on d.id = o.inline_datum_id
"""


def _assets(cur, tx_out_id: int, lovelace: int) -> dict[str, int]:
    assets: dict[str, int] = {"lovelace": int(lovelace)}
    cur.execute(
        """select encode(ma.policy, 'hex') || encode(ma.name, 'hex'), mto.quantity
           from ma_tx_out mto
           join multi_asset ma on ma.id = mto.ident
           where mto.tx_out_id = %s
           order by 1""",
        (tx_out_id,),
    )
    for unit, quantity in cur.fetchall():
        assets[unit] = int(quantity)
    return assets


_HOLDS_ASSET = """o.id in (
    select mto.tx_out_id from ma_tx_out mto
    join multi_asset ma on ma.id = mto.ident
    where ma.policy = decode(%s, 'hex') and ma.name = decode(%s, 'hex'))"""


def _records(cur, where: str, args: tuple, *, by_out_ref: bool = True) -> list[dict]:
    cur.execute(_RECORD_SQL + " where " + where + " order by o.id", args)
    out = []
    for tx_hash, ix, address, lovelace, datum_hex, tx_out_id in cur.fetchall():
        out.append(
            dict(
                out_ref=f"{tx_hash}#{ix}",
                address=address,
                datum_cbor=datum_hex or "",
                assets=_assets(cur, tx_out_id, lovelace),
            ),
        )
    return sorted(out, key=lambda r: r["out_ref"]) if by_out_ref else out


def capture() -> dict:
    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    out: dict = {"config": _records(cur, _HOLDS_ASSET, CONFIG_NFT, by_out_ref=False)}
    (out["lender_manager_config"],) = _records(
        cur,
        "o.consumed_by_tx_id is null and " + _HOLDS_ASSET,
        LENDER_MANAGER_CONFIG_NFT,
    )
    for entity, credential in ENTITY_CREDENTIALS.items():
        out[entity] = _records(
            cur,
            "o.consumed_by_tx_id is null and a.payment_cred = decode(%s, 'hex')",
            (credential,),
        )
    conn.close()
    return out


if __name__ == "__main__":
    here = Path(__file__).parent
    data = capture()
    (here / "entities.json").write_text(json.dumps(data, indent=2) + "\n")
    for key, value in data.items():
        count = len(value) if isinstance(value, list) else 1
        print(f"{key}: {count}")
