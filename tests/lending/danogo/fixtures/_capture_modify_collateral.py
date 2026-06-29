"""Dev-only: capture real modify-collateral txs from dbsync into JSON fixtures.

A modify-collateral tx adds and/or removes collateral on an EXISTING loan WITHOUT
repaying: the loan token (qty 1) + owner NFT are preserved, the loan datum is
byte-UNCHANGED in -> out, and only the loan UTxO's locked collateral value moves. It
is wired as ``Spend(loan_skh)`` (carrying the ``ModifyCollaterals`` redeemer, loan
validator alt index 9 / CBOR tag 1282 / hex prefix ``d90502``) + ``Withdraw(oracle_skh)``
ONLY. The pool is a REFERENCE input (read for the interest index at ``pool_ref_idx``),
NOT spent. There is no pool spend, no fee output, and no mint.

The fixture embeds enough to (a) byte-exact test the redeemer + assert the loan datum
passthrough and (b) forward-replay against the spent inputs via Ogmios
``additionalUtxo``: full tx CBOR, every resolved spent input (loan w/ datum +
collateral value + out-ref; borrower w/ owner NFT) with address/value/inline datum,
all reference inputs (incl. the pool ref + ref scripts), the decoded
``ModifyCollaterals`` redeemer, the oracle redeemer, the loan_in + loan_out datums and
collateral maps, the pool-ref out-ref, and the validity window.

Run: PYTHONPATH=src python tests/lending/danogo/fixtures/_capture_modify_collateral.py
Requires dbsync env (DBSYNC_*) + a Blockfrost PROJECT_ID (the local dbsync prunes
consumed tx_out/tx_in rows, so spent inputs are resolved via Blockfrost). Not run in CI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cbor2
import psycopg

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket

# The CBOR tag of ModifyCollaterals in the loan validator's redeemer union (alt index
# 9 == CBOR tag 1282 == hex prefix ``d90502``). The loan Spend redeemer carrying it is
# the selector that distinguishes modify-collateral txs from every other loan action.
MODIFY_COLLATERAL_CONSTR_TAG = 1282
MODIFY_COLLATERAL_CBOR_PREFIX = "d90502"

# Chosen mainnet modify-collateral transactions (standalone: loan Spend + oracle
# Withdraw, pool referenced not spent, no mint). Overridable via env.
#  - ADD-only      (structured oracle 2eb7e9be)
#  - REMOVE-only    (legacy oracle 012a6bd4)
#  - TYPE-SWAP      (add + remove / cross collateral type)
ADD_TX = os.environ.get(
    "MODIFY_COLLATERAL_ADD_TX",
    "62f6fa25ed49bd0e55e21ce5bf236873e7106c33eb30214ff39981bed8f656f4",
)
REMOVE_TX = os.environ.get(
    "MODIFY_COLLATERAL_REMOVE_TX",
    "cd72844bbf760e655e4e66217fcb73bae017be4cdb30868db5c2c6ebb0c25c37",
)
SWAP_TX = os.environ.get(
    "MODIFY_COLLATERAL_SWAP_TX",
    "0fc38568783bdd8bbe5d76c1b2910c254bb5ea4a4dbc9d87fe8eeb3fe3620b4f",
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


def _parses(datum_hex, cls) -> bool:
    if not datum_hex:
        return False
    try:
        cls.from_cbor(bytes.fromhex(datum_hex))
        return True
    except Exception:  # noqa: BLE001
        return False


def _decode_redeemer(cbor_hex: str) -> dict:
    """Decode a ModifyCollaterals redeemer (alt index 9) into its named fields.

    Four plain ``int`` index fields and no ``pool_in_out_ref``: the pool is referenced
    by ``pool_ref_idx``, not consumed, so the redeemer carries no spent-pool out-ref.
    """
    tag = cbor2.loads(bytes.fromhex(cbor_hex))
    assert (
        isinstance(tag, cbor2.CBORTag) and tag.tag == MODIFY_COLLATERAL_CONSTR_TAG
    ), "expected ModifyCollaterals (alt index 9 / CBOR tag 1282)"
    loan_out_idx, cfg_idx, market_idx, pool_ref_idx = tag.value
    return dict(
        constr=tag.tag - 1280 + 7,
        loan_out_idx=loan_out_idx,
        protocol_cfg_ref_idx=cfg_idx,
        market_ref_idx=market_idx,
        pool_ref_idx=pool_ref_idx,
    )


def _collateral_map(utxo: dict, *, loan_skh: str, market_name: str) -> dict:
    """The loan UTxO's locked collateral as a unit->qty map.

    Every native asset the loan UTxO holds except the market loan token
    (``loan_skh`` + ``market_name``) is collateral; the lovelace (min-ADA) balance is
    not collateral and is excluded. Mirrors ``_loan_collateral_units`` in the builder.
    """
    return {
        policy + name: int(qty)
        for policy, name, qty in utxo["assets"]
        if not (policy == loan_skh and name == market_name)
    }


def _collateral_delta(
    loan_in: dict,
    loan_out: dict,
    *,
    loan_skh: str,
    market_name: str,
) -> dict:
    """Per-unit collateral change (out - in) plus the target (loan_out) map.

    ``new_collateral_units`` flags collateral units present in the OUTPUT but not the
    INPUT (a genuinely new collateral TYPE that needs fresh oracle leaves downstream);
    ``removed_collateral_units`` flags units dropped entirely from the loan.
    """
    collat_in = _collateral_map(loan_in, loan_skh=loan_skh, market_name=market_name)
    collat_out = _collateral_map(loan_out, loan_skh=loan_skh, market_name=market_name)
    units = sorted(set(collat_in) | set(collat_out))
    delta = {
        unit: collat_out.get(unit, 0) - collat_in.get(unit, 0)
        for unit in units
        if collat_out.get(unit, 0) - collat_in.get(unit, 0) != 0
    }
    return dict(
        collateral_in=collat_in,
        collateral_out=collat_out,
        collateral_delta=delta,
        new_collateral_units=[u for u in collat_out if u not in collat_in],
        removed_collateral_units=[u for u in collat_in if u not in collat_out],
        collateral_modified=bool(delta),
    )


def capture(tx_hash: str, *, variant: str) -> dict:
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

    # The redeemer's reference-input indices address the CANONICALLY sorted reference
    # set (by ``(tx_id, index)``, as Plutus presents ``tx_info.reference_inputs``), so
    # store + index the reference inputs in that order.
    ref_inputs.sort(key=lambda u: (u["out_ref"][0], u["out_ref"][1]))

    # Spent inputs: this dbsync prunes consumed rows, so resolve via Blockfrost.
    inputs = _inputs_from_blockfrost(tx_hash, project_id)
    tx_cbor = _tx_cbor(tx_hash, project_id)

    # A modify-collateral tx mints nothing.
    assert not mints, "a modify-collateral tx mints nothing"

    # The ModifyCollaterals redeemer rides the loan Spend (NOT a Withdraw hub); select
    # it by its CBOR prefix to stay agnostic to dbsync's per-purpose script_hash.
    modify_cbor = next(
        r["cbor"]
        for r in redeemers
        if r["purpose"] == "spend"
        and r["cbor"].startswith(MODIFY_COLLATERAL_CBOR_PREFIX)
    )
    decoded = _decode_redeemer(modify_cbor)

    # Resolve the protocol-config / pool / market reference UTxOs by the redeemer's
    # own indices (a cross-type swap references >1 pool, so a blind "first PoolDatum"
    # scan would pick the wrong one). Each must parse as its expected shape.
    protocol_config = ref_inputs[decoded["protocol_cfg_ref_idx"]]
    pool_ref = ref_inputs[decoded["pool_ref_idx"]]
    market = ref_inputs[decoded["market_ref_idx"]]
    assert _parses(protocol_config["datum"], ProtocolDatum), "cfg idx not ProtocolDatum"
    assert _parses(pool_ref["datum"], PoolDatum), "pool_ref idx not PoolDatum"

    pd = ProtocolDatum.from_cbor(protocol_config["datum"])
    pool_skh = pd.pool_skh.hex()
    loan_skh = pd.loan_skh.hex()
    config_pool_skh = pd.config_pool_skh.hex()
    oracle_skh = pd.oracle_skh.hex()

    market_name = next(
        a[1] for a in pool_ref["assets"] if a[0] == config_pool_skh and int(a[2]) == 1
    )
    market_info = DanogoMarket.from_market_datum(market["datum"])

    # Loan input + loan output (both present: the loan is never closed on a modify).
    loan_in = next(u for u in inputs if _parses(u["datum"], LoanDatum))
    loan_out_entry = outputs[decoded["loan_out_idx"]]
    assert _parses(loan_out_entry["datum"], LoanDatum), "loan_out idx not a LoanDatum"

    oracle_cbor = next(
        (
            r["cbor"]
            for r in redeemers
            if r["purpose"] == "reward" and r["script_hash"] == oracle_skh
        ),
        None,
    )

    # No pool is spent: assert there is no pool-script spend among the redeemers.
    assert not any(
        r["purpose"] == "spend" and r["script_hash"] == pool_skh for r in redeemers
    ), "a standalone modify-collateral tx does not spend the pool"

    loan_in_datum_obj = LoanDatum.from_cbor(loan_in["datum"])
    loan_out_datum_obj = LoanDatum.from_cbor(loan_out_entry["datum"])

    # The loan datum is byte-UNCHANGED in -> out (only the collateral value moves).
    assert loan_in["datum"] == loan_out_entry["datum"], (
        "loan datum changed across a modify-collateral tx (expected passthrough): "
        f"{loan_in['datum']} != {loan_out_entry['datum']}"
    )

    supply_token = loan_in_datum_obj.token_unit()
    collateral = _collateral_delta(
        loan_in,
        loan_out_entry,
        loan_skh=loan_skh,
        market_name=market_name,
    )
    assert collateral["collateral_modified"], "no collateral change detected"

    # The borrower input carries the owner NFT (qty 1) that authorizes the loan.
    owner_unit = loan_in_datum_obj.owner_nft.unit()
    owner_policy, owner_name = owner_unit[:56], owner_unit[56:]
    borrower_in = next(
        (
            u
            for u in inputs
            if any(
                a[0] == owner_policy and a[1] == owner_name and int(a[2]) == 1
                for a in u["assets"]
            )
        ),
        None,
    )

    pool_ref_datum_obj = PoolDatum.from_cbor(pool_ref["datum"])

    return dict(
        variant=variant,
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
        mints=mints,
        redeemers=redeemers,
        inputs=inputs,
        ref_inputs=ref_inputs,
        outputs={str(k): v for k, v in outputs.items()},
        modify_collateral_redeemer=modify_cbor,
        modify_collateral_redeemer_decoded=decoded,
        oracle_redeemer=oracle_cbor,
        owner_nft=owner_unit,
        borrower_in_out_ref=borrower_in["out_ref"] if borrower_in else None,
        pool_ref_out_ref=pool_ref["out_ref"],
        pool_ref_datum=pool_ref["datum"],
        pool_ref_interest_index=pool_ref_datum_obj.interest_index,
        pool_ref_interest_time=pool_ref_datum_obj.interest_time,
        loan_in_out_ref=loan_in["out_ref"],
        loan_in_datum=loan_in["datum"],
        loan_out_datum=loan_out_entry["datum"],
        loan_datum_unchanged=loan_in["datum"] == loan_out_entry["datum"],
        loan_in_amount=loan_in_datum_obj.loan_amount,
        loan_in_initial_index=loan_in_datum_obj.initial_interest_index,
        collateral=collateral,
        tx_cbor=tx_cbor,
    )


if __name__ == "__main__":
    here = Path(__file__).parent
    jobs = [
        (ADD_TX, "add", "modify_collateral_add_tx.json"),
        (REMOVE_TX, "remove", "modify_collateral_remove_tx.json"),
        (SWAP_TX, "swap", "modify_collateral_swap_tx.json"),
    ]
    for tx_hash, variant, filename in jobs:
        data = capture(tx_hash, variant=variant)
        (here / filename).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(
            f"wrote {filename} ({variant}, tx {data['tx_hash']}, "
            f"oracle {data['oracle_skh']}, "
            f"delta {data['collateral']['collateral_delta']})"
        )
