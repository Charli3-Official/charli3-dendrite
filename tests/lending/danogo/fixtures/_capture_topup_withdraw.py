"""Dev-only: capture a real deposit/withdraw (TopupWithdraw) tx into a JSON fixture.

Captures a clean single-pool TopupWithdraw on the large ADA-supply pool whose market
lists two alternative supply tokens: one held + oracle-priced (re-priced on every pool
action) and one disallowed + never held + with no pricing recipe (its rate is carried
unchanged). The fixture embeds enough to (a) byte-exact reconstruct the synthesized
pool datum -- exercising the alt-supply revaluation that re-prices the held token and
carries the un-priceable one -- and (b) forward-build + Ogmios-evaluate a fresh deposit
against the captured spent inputs via ``additionalUtxo``: full tx CBOR, every resolved
input (pool + actor) with address/value/inline datum, reference inputs (config / market
/ oracle config + path + source leaves + the reference scripts), the decoded redeemers,
the pool_in/pool_out datums, mint entries, and a ``realized`` block of computed deltas.

Run: PYTHONPATH=src python tests/lending/danogo/fixtures/_capture_topup_withdraw.py
Requires dbsync env (DBSYNC_*) + a Blockfrost PROJECT_ID (the local dbsync prunes
consumed tx_out/tx_in rows, so spent inputs are resolved via Blockfrost). Not run in CI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import psycopg

from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket

PROTOCOL_CONFIG_NFT = (
    "bc2d3b7cf1009c788b1daf2208a38026ab20d7f8209d896ad1a09c14"
    "e567b64450c406a17f869eb4e430f2e2a6c74d103edb988cb1bc1347de285c69"
)
POOL_SCRIPT_SKH = "94dca24a1f1fcc2ff51cd90f32f4fe9e786d861a2dbf7d27598d26e8"

# A clean single-pool withdraw on the large ADA pool (market f04403...3803a14): pool
# Spend + dToken burn + oracle Withdraw, in which the held alt token (73f29518...) is
# re-priced (its datum rate changes in -> out) while the disallowed zero-held token
# (9759bfd8...) is carried unchanged.
TOPUP_TX = os.environ.get(
    "DANOGO_CAPTURE_TOPUP_TX",
    "e4cef71250414c522ffed572398464ac88dfc91f349280c380b9759d2ab9070e",
)

CONN = dict(
    host=os.environ.get("DBSYNC_HOST", "pop-os"),
    port=int(os.environ.get("DBSYNC_PORT", "5432")),
    dbname=os.environ.get("DBSYNC_DB_NAME", "cexplorer"),
    user=os.environ.get("DBSYNC_USER", "cexplorer"),
    password=os.environ.get("DBSYNC_PASS", ""),
    connect_timeout=30,
)


def _utxo(cur, tx_out_id: int) -> dict:
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


def _tx_cbor(tx_hash: str, project_id: str) -> str | None:
    import requests  # noqa: PLC0415

    resp = requests.get(
        f"https://cardano-mainnet.blockfrost.io/api/v0/txs/{tx_hash}/cbor",
        headers={"project_id": project_id},
        timeout=30,
    )
    return resp.json().get("cbor") if resp.ok else None


def _holds(utxo: dict, policy: str, name: str) -> bool:
    return any(a[0] == policy and a[1] == name for a in utxo["assets"])


def _parses(datum_hex, cls) -> bool:
    if not datum_hex:
        return False
    try:
        cls.from_cbor(bytes.fromhex(datum_hex))
        return True
    except Exception:  # noqa: BLE001
        return False


def _supply_qty(utxo: dict, supply_unit: str) -> int:
    """Quantity of the supply token a UTxO holds (its lovelace balance for ADA)."""
    if supply_unit == "lovelace":
        return int(utxo["lovelace"])
    policy, name = supply_unit[:56], supply_unit[56:]
    return next(
        (int(a[2]) for a in utxo["assets"] if a[0] == policy and a[1] == name),
        0,
    )


def capture(tx_hash: str) -> dict:
    project_id = os.environ.get("PROJECT_ID")
    if not project_id:
        raise RuntimeError("PROJECT_ID (Blockfrost) required to resolve spent inputs")

    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    cur.execute(
        """SELECT t.id, t.invalid_before, t.invalid_hereafter, t.fee, b.time
           FROM tx t JOIN block b ON b.id=t.block_id
           WHERE t.hash=decode(%s,'hex')""",
        (tx_hash,),
    )
    tid, inv_before, inv_after, fee, block_time = cur.fetchone()

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

    cur.execute(
        """SELECT encode(ma.policy,'hex'), encode(ma.name,'hex'), m.quantity
           FROM ma_tx_mint m JOIN multi_asset ma ON ma.id=m.ident WHERE m.tx_id=%s""",
        (tid,),
    )
    mints = [[p, n, int(q)] for p, n, q in cur.fetchall()]

    cur.execute("SELECT id, index FROM tx_out WHERE tx_id=%s ORDER BY index", (tid,))
    outputs = {idx: _utxo(cur, oid) for oid, idx in cur.fetchall()}

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

    inputs = _inputs_from_blockfrost(tx_hash, project_id)
    tx_cbor = _tx_cbor(tx_hash, project_id)

    # Derive the script hashes from the protocol-config reference UTxO.
    cfg_policy, cfg_name = PROTOCOL_CONFIG_NFT[:56], PROTOCOL_CONFIG_NFT[56:]
    protocol_config = next(u for u in ref_inputs if _holds(u, cfg_policy, cfg_name))
    pd = ProtocolDatum.from_cbor(protocol_config["datum"])
    pool_skh = pd.pool_skh.hex()
    loan_skh = pd.loan_skh.hex()
    config_pool_skh = pd.config_pool_skh.hex()
    oracle_skh = pd.oracle_skh.hex()

    # Pool input/output (parse PoolDatum), and the market_name = pool-NFT asset name.
    pool_in = next(u for u in inputs if _parses(u["datum"], PoolDatum))
    market_name = next(
        a[1] for a in pool_in["assets"] if a[0] == config_pool_skh and int(a[2]) == 1
    )
    pool_out_entry = next(
        u
        for u in outputs.values()
        if u["address"] == pool_in["address"] and _parses(u["datum"], PoolDatum)
    )

    # Market-param reference UTxO: holds the pool NFT but is not the pool itself.
    market = next(
        u
        for u in ref_inputs
        if _holds(u, config_pool_skh, market_name)
        and u["address"] != pool_in["address"]
    )
    market_info = DanogoMarket.from_market_datum(market["datum"])
    supply_token = market_info.supply_token

    topup_cbor = next(
        r["cbor"]
        for r in redeemers
        if r["purpose"] == "spend" and r["script_hash"] == pool_skh
    )
    oracle_cbor = next(
        (
            r["cbor"]
            for r in redeemers
            if r["purpose"] == "reward" and r["script_hash"] == oracle_skh
        ),
        None,
    )

    pool_in_datum_obj = PoolDatum.from_cbor(pool_in["datum"])
    pool_out_datum_obj = PoolDatum.from_cbor(pool_out_entry["datum"])

    pool_in_supply = _supply_qty(pool_in, supply_token)
    pool_out_supply = _supply_qty(pool_out_entry, supply_token)
    pool_changed_amount = pool_out_supply - pool_in_supply
    dtoken_unit_delta = next(
        (q for p, n, q in mints if p == pool_skh and n == market_name),
        0,
    )

    realized = dict(
        txn_time=pool_out_datum_obj.interest_time,
        base_rate=market_info.base_rate,
        power_base=market_info.power_base,
        loan_fee_rate=market_info.loan_fee_rate,
        withdraw_fee=0,
        pool_changed_amount=pool_changed_amount,
        mint_burn_dtoken=dtoken_unit_delta,
        pool_in_supply=pool_in_supply,
        pool_out_supply=pool_out_supply,
        pool_in_alt_rates=[
            [r.num, r.denom] for r in pool_in_datum_obj.alt_supply_tokens_rate
        ],
        pool_out_alt_rates=[
            [r.num, r.denom] for r in pool_out_datum_obj.alt_supply_tokens_rate
        ],
    )

    return dict(
        tx_hash=tx_hash,
        block_time=int(block_time.timestamp())
        if hasattr(block_time, "timestamp")
        else int(block_time),
        fee=int(fee),
        invalid_before=int(inv_before) if inv_before is not None else None,
        invalid_hereafter=int(inv_after) if inv_after is not None else None,
        loan_skh=loan_skh,
        pool_skh=pool_skh,
        config_pool_skh=config_pool_skh,
        oracle_skh=oracle_skh,
        market_name=market_name,
        supply_token=supply_token,
        alt_supply_market=bool(market_info.alt_supply_tokens),
        alt_supply_tokens={u: a for u, a in market_info.alt_supply_tokens.items()},
        mints=mints,
        redeemers=redeemers,
        inputs=inputs,
        ref_inputs=ref_inputs,
        outputs={str(k): v for k, v in outputs.items()},
        topup_withdraw_redeemer=topup_cbor,
        oracle_redeemer=oracle_cbor,
        pool_in_out_ref=[pool_in["out_ref"][0], pool_in["out_ref"][1]],
        pool_in_datum=pool_in["datum"],
        pool_out_datum=pool_out_entry["datum"],
        realized=realized,
        tx_cbor=tx_cbor,
    )


if __name__ == "__main__":
    here = Path(__file__).parent
    fix = capture(TOPUP_TX)
    (here / "topup_zero_held_alt_tx.json").write_text(
        json.dumps(fix, indent=2, sort_keys=True) + "\n"
    )
    print(
        "wrote topup_zero_held_alt_tx.json "
        f"(tx {fix['tx_hash']}, market {fix['market_name']}, "
        f"pool_changed {fix['realized']['pool_changed_amount']})"
    )
    print("  alt tokens:", fix["alt_supply_tokens"])
    print("  pool_in  alt_rates:", fix["realized"]["pool_in_alt_rates"])
    print("  pool_out alt_rates:", fix["realized"]["pool_out_alt_rates"])
