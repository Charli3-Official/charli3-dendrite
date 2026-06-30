"""Dev-only: capture real FluidTokens loan-action txs from dbsync into JSON fixtures.

Captures everything an Ogmios forward-build replay needs for a loan action (repay /
change-collateral / recast): the validity window, the spent inputs (resolved via
``tx_out.consumed_by_tx_id`` -- this dbsync retains consumed rows, so no Blockfrost is
needed), the outputs, the canonically-sorted reference inputs (with attached reference
scripts), the mints, and every redeemer. The loan input / output datums are surfaced
so the byte-exact datum-synth tests can assert against them.

Run: PYTHONPATH=src python tests/lending/fluidtokens/transactions/fixtures/_capture_loan_action.py
Requires dbsync env (DBSYNC_*). Not run in CI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg

from charli3_dendrite.lending.fluidtokens.datums import LoanDatum

CONN = dict(
    host=os.environ.get("DBSYNC_HOST", "localhost"),
    port=int(os.environ.get("DBSYNC_PORT", "5432")),
    dbname=os.environ.get("DBSYNC_DB_NAME", "cexplorer"),
    user=os.environ.get("DBSYNC_USER", "cexplorer"),
    password=os.environ.get("DBSYNC_PASS", ""),
    connect_timeout=30,
)


def _assets(cur, tx_out_id: int) -> list[list[str]]:
    cur.execute(
        """SELECT encode(ma.policy,'hex'), encode(ma.name,'hex'), m.quantity
           FROM ma_tx_out m JOIN multi_asset ma ON ma.id=m.ident
           WHERE m.tx_out_id=%s ORDER BY ma.policy, ma.name""",
        (tx_out_id,),
    )
    return [[p, n, str(q)] for p, n, q in cur.fetchall()]


def _utxo(cur, tx_out_id: int) -> dict:
    cur.execute(
        """SELECT o.value, a.address, encode(d.bytes,'hex'),
                  encode(s.bytes,'hex'), s.type, o.index, encode(t.hash,'hex')
           FROM tx_out o
           JOIN address a ON a.id=o.address_id
           LEFT JOIN datum d ON d.id=o.inline_datum_id
           LEFT JOIN script s ON s.id=o.reference_script_id
           JOIN tx t ON t.id=o.tx_id
           WHERE o.id=%s""",
        (tx_out_id,),
    )
    value, addr, datum, script, stype, idx, txh = cur.fetchone()
    return dict(
        lovelace=int(value),
        address=addr,
        datum=datum,
        ref_script=script,
        ref_script_type=stype,
        out_ref=[txh, int(idx)],
        assets=_assets(cur, tx_out_id),
    )


def _is_loan(datum_hex: str | None) -> bool:
    if not datum_hex:
        return False
    try:
        LoanDatum.from_cbor(bytes.fromhex(datum_hex))
        return True
    except Exception:  # noqa: BLE001
        return False


def capture(tx_hash: str, *, label: str) -> dict:
    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    cur.execute(
        """SELECT t.id, t.invalid_before, t.invalid_hereafter, t.fee, b.time
           FROM tx t JOIN block b ON b.id=t.block_id
           WHERE t.hash=decode(%s,'hex')""",
        (tx_hash,),
    )
    tid, inv_before, inv_after, fee, block_time = cur.fetchone()

    # Spent inputs via consumed_by_tx_id (retained on this dbsync).
    cur.execute(
        "SELECT id FROM tx_out WHERE consumed_by_tx_id=%s ORDER BY id",
        (tid,),
    )
    inputs = [_utxo(cur, r[0]) for r in cur.fetchall()]

    cur.execute("SELECT id FROM tx_out WHERE tx_id=%s ORDER BY index", (tid,))
    outputs = [_utxo(cur, r[0]) for r in cur.fetchall()]

    cur.execute(
        """SELECT o.id FROM reference_tx_in r
           JOIN tx_out o ON o.tx_id=r.tx_out_id AND o.index=r.tx_out_index
           WHERE r.tx_in_id=%s""",
        (tid,),
    )
    ref_inputs = [_utxo(cur, r[0]) for r in cur.fetchall()]
    # The Plutus script context exposes reference_inputs sorted by (tx_id, index).
    ref_inputs.sort(key=lambda u: (u["out_ref"][0], u["out_ref"][1]))

    cur.execute(
        """SELECT encode(ma.policy,'hex'), encode(ma.name,'hex'), m.quantity
           FROM ma_tx_mint m JOIN multi_asset ma ON ma.id=m.ident WHERE m.tx_id=%s""",
        (tid,),
    )
    mints = [[p, n, int(q)] for p, n, q in cur.fetchall()]

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
    conn.close()

    loan_in = next((u for u in inputs if _is_loan(u["datum"])), None)
    loan_out = next((u for u in outputs if _is_loan(u["datum"])), None)

    return dict(
        label=label,
        tx_hash=tx_hash,
        block_time=(
            int(block_time.timestamp())
            if hasattr(block_time, "timestamp")
            else int(block_time)
        ),
        fee=int(fee),
        invalid_before=int(inv_before) if inv_before is not None else None,
        invalid_hereafter=int(inv_after) if inv_after is not None else None,
        mints=mints,
        redeemers=redeemers,
        inputs=inputs,
        outputs=outputs,
        ref_inputs=ref_inputs,
        loan_in_out_ref=loan_in["out_ref"] if loan_in else None,
        loan_in_datum=loan_in["datum"] if loan_in else None,
        loan_out_datum=loan_out["datum"] if loan_out else None,
    )


JOBS = {
    "repay_full": "993715116500e7330b86dd6f9eeda1236a59fe58642b4d21c55afc7c2f482cdb",
    "change_collateral": (
        "142d5299952aaafe4f6b08a6ad655345663f536576bac3d8bd48e71b6be9e09d"
    ),
    "recast": "f4c3e7ebc6e1cc249a7bc5187e5e9581c2dc9eddd8276ae244e6821320377f26",
    # Pool-origin borrow: spends a pool UTxO (empty redeemer), mints loan + borrower
    # bond + lender bond (asset name = hash of the pool out-ref), continues the pool,
    # dynamic SNEK collateral priced via one signed oracle reward, ADA principal.
    "borrow_pool": ("3733cbee1c8cca80acaf00bb09c675c8a89a09d6363a2e92df6b2065be692314"),
    # Create a borrow request: mints one request NFT (asset name = 0x00 ++ hash of the
    # chosen input out-ref) and locks it + the collateral + an inline RequestDatum at the
    # request spend address. Only the request mint policy runs.
    "create_request": (
        "9c6baee9f46604e1b31c5af201fb4e9ebaaf453d1b63becc64097abf029679bb"
    ),
    # Cancel a borrow request: spends the request UTxO (empty redeemer), burns the
    # request NFT, and drives the request-policy reward (``Cancel``) authorized by the
    # borrower signature; the collateral returns to the borrower.
    "cancel_request": (
        "bbb39d5d0953d7c1560c53b5b66b41c165ec2ee5d1a25d5d2401de0d42d9733f"
    ),
}


if __name__ == "__main__":
    here = Path(__file__).parent
    for label, tx in JOBS.items():
        data = capture(tx, label=label)
        (here / f"{label}.json").write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
        )
        n_loan_out = "loan_out" if data["loan_out_datum"] else "no-loan-out"
        print(
            f"wrote {label}.json (tx {tx[:12]}, {len(data['inputs'])} inputs, "
            f"{len(data['outputs'])} outputs, {len(data['ref_inputs'])} refs, "
            f"mints={len(data['mints'])}, {n_loan_out})",
        )
