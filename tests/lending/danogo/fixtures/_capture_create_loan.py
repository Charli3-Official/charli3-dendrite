"""Dev-only: capture one real create-loan tx from dbsync into create_loan_tx.json.

Run: python tests/lending/danogo/fixtures/_capture_create_loan.py
Requires dbsync env (DBSYNC_* or the hardcoded CONN below). Not run in CI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg

ORACLE_SKH = "012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a"
CONN = dict(
    host=os.environ.get("DBSYNC_HOST", "pop-os"),
    port=int(os.environ.get("DBSYNC_PORT", "5432")),
    dbname=os.environ.get("DBSYNC_DB_NAME", "cexplorer"),
    user=os.environ.get("DBSYNC_USER", "cexplorer"),
    password=os.environ.get(
        "DBSYNC_PASS", "x23HgZklGl8LHNEAUkUoXFLUAnb798Bi13LjLuQ4cqc"
    ),
    connect_timeout=20,
)


def _utxo(cur, tx_out_id):
    cur.execute(
        """SELECT o.value, a.address, encode(d.bytes,'hex'),
                  encode(s.bytes,'hex'), s.type
           FROM tx_out o
           JOIN address a ON a.id=o.address_id
           LEFT JOIN datum d ON d.id=o.inline_datum_id
           LEFT JOIN script s ON s.id=o.reference_script_id
           WHERE o.id=%s""",
        (tx_out_id,),
    )
    value, addr, datum, script, stype = cur.fetchone()
    cur.execute(
        """SELECT encode(ma.policy,'hex'), encode(ma.name,'hex'), m.quantity
           FROM ma_tx_out m JOIN multi_asset ma ON ma.id=m.ident
           WHERE m.tx_out_id=%s ORDER BY ma.policy, ma.name""",
        (tx_out_id,),
    )
    assets = [[p, n, str(q)] for p, n, q in cur.fetchall()]
    return dict(
        lovelace=int(value),
        address=addr,
        datum=datum,
        ref_script=script,
        ref_script_type=stype,
        assets=assets,
    )


def _inputs_from_blockfrost(tx_hash: str, project_id: str) -> list:
    """Resolve a tx's spent (non-collateral, non-reference) inputs via Blockfrost."""
    import requests  # noqa: PLC0415

    resp = requests.get(
        f"https://cardano-mainnet.blockfrost.io/api/v0/txs/{tx_hash}/utxos",
        headers={"project_id": project_id},
        timeout=30,
    )
    resp.raise_for_status()
    inputs = []
    for entry in resp.json().get("inputs", []):
        if entry.get("collateral") or entry.get("reference"):
            continue
        assets = [
            [a["unit"][:56], a["unit"][56:], a["quantity"]]
            for a in entry["amount"]
            if a["unit"] != "lovelace"
        ]
        lovelace = next(
            (int(a["quantity"]) for a in entry["amount"] if a["unit"] == "lovelace"),
            0,
        )
        inputs.append(
            dict(
                lovelace=lovelace,
                address=entry["address"],
                datum=entry.get("inline_datum"),
                ref_script=None,
                ref_script_type=None,
                assets=assets,
                out_ref=[entry["tx_hash"], int(entry["output_index"])],
            )
        )
    return inputs


def capture() -> dict:
    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    cur.execute(
        """SELECT t.id, encode(t.hash,'hex'), t.invalid_before,
                  t.invalid_hereafter, t.fee
           FROM redeemer r JOIN tx t ON t.id=r.tx_id JOIN block b ON b.id=t.block_id
           WHERE r.purpose='reward' AND r.script_hash=decode(%s,'hex')
             AND EXISTS (SELECT 1 FROM ma_tx_mint m WHERE m.tx_id=t.id)
           ORDER BY b.time DESC LIMIT 1000""",
        (ORACLE_SKH,),
    )
    candidates = cur.fetchall()
    chosen = None
    for tid, txh, inv_before, inv_after, fee in candidates:
        cur.execute(
            """SELECT encode(ma.policy,'hex'), encode(ma.name,'hex'), m.quantity
               FROM ma_tx_mint m JOIN multi_asset ma ON ma.id=m.ident
               WHERE m.tx_id=%s""",
            (tid,),
        )
        mints = [(p, n, int(q)) for p, n, q in cur.fetchall()]
        qty1 = [m for m in mints if m[2] == 1]
        policies = {m[0] for m in qty1}
        if len(qty1) == 2 and len(policies) == 1 and len(mints) == 2:
            chosen = (tid, txh, inv_before, inv_after, fee, mints, policies.pop())
            break
    if chosen is None:
        raise RuntimeError("no clean create-loan tx found in candidate window")
    tid, txh, inv_before, inv_after, fee, mints, loan_skh = chosen

    cur.execute(
        """SELECT r.purpose, encode(r.script_hash,'hex'), r.index,
                  encode(rd.bytes,'hex')
           FROM redeemer r JOIN redeemer_data rd ON rd.id=r.redeemer_data_id
           WHERE r.tx_id=%s ORDER BY r.purpose, r.index""",
        (tid,),
    )
    redeemers = [
        dict(purpose=pu, script_hash=sh, index=ix, cbor=cb)
        for pu, sh, ix, cb in cur.fetchall()
    ]

    cur.execute("SELECT id, index FROM tx_out WHERE tx_id=%s ORDER BY index", (tid,))
    outputs = {idx: _utxo(cur, oid) for oid, idx in cur.fetchall()}

    cur.execute(
        """SELECT o.id, encode(pt.hash,'hex'), o.index
           FROM tx_in i JOIN tx_out o
             ON o.tx_id=i.tx_out_id AND o.index=i.tx_out_index
           JOIN tx pt ON pt.id=o.tx_id
           WHERE i.tx_in_id=%s ORDER BY i.id""",
        (tid,),
    )
    inputs = []
    for oid, prev_hash, prev_idx in cur.fetchall():
        u = _utxo(cur, oid)
        u["out_ref"] = [prev_hash, int(prev_idx)]
        inputs.append(u)

    # This dbsync prunes consumed tx_out rows, so spent inputs are unresolvable via the
    # join above. Fall back to Blockfrost /txs/{hash}/utxos (non-pruned, inline datums).
    if not inputs and os.environ.get("PROJECT_ID"):
        inputs = _inputs_from_blockfrost(txh, os.environ["PROJECT_ID"])

    cur.execute(
        """SELECT o.id, encode(pt.hash,'hex'), o.index
           FROM reference_tx_in r JOIN tx_out o
             ON o.tx_id=r.tx_out_id AND o.index=r.tx_out_index
           JOIN tx pt ON pt.id=o.tx_id
           WHERE r.tx_in_id=%s ORDER BY r.id""",
        (tid,),
    )
    ref_inputs = []
    for oid, ref_hash, ref_idx in cur.fetchall():
        u = _utxo(cur, oid)
        u["out_ref"] = [ref_hash, int(ref_idx)]
        ref_inputs.append(u)
    conn.close()

    tx_cbor = None
    pid = os.environ.get("PROJECT_ID", "")
    if pid:
        import requests  # noqa: PLC0415

        resp = requests.get(
            f"https://cardano-mainnet.blockfrost.io/api/v0/txs/{txh}/cbor",
            headers={"project_id": pid},
            timeout=30,
        )
        if resp.ok:
            tx_cbor = resp.json().get("cbor")

    return dict(
        tx_hash=txh,
        loan_skh=loan_skh,
        fee=int(fee),
        invalid_before=int(inv_before) if inv_before is not None else None,
        invalid_hereafter=int(inv_after) if inv_after is not None else None,
        mints=[[p, n, q] for p, n, q in mints],
        redeemers=redeemers,
        inputs=inputs,
        ref_inputs=ref_inputs,
        outputs={str(k): v for k, v in outputs.items()},
        tx_cbor=tx_cbor,
    )


if __name__ == "__main__":
    data = capture()
    out = Path(__file__).parent / "create_loan_tx.json"
    out.write_text(json.dumps(data, indent=2, sort_keys=True))
    print(f"wrote {out} (tx {data['tx_hash']})")
