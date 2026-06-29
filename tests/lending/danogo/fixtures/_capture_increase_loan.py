"""Dev-only: capture a real increase-loan tx from dbsync into a JSON fixture.

An increase-loan borrows MORE supply token against an EXISTING loan (same loan
token + owner NFT, no new loan, no mint). It is wired as Spend(pool) + Spend(loan)
delegated through a ``Withdraw(pool_skh)`` reward hub (NOT repay's
``Withdraw(loan_skh)``), plus ``Withdraw(oracle_skh)`` to price collateral. The same
``IncreaseLoanAmount`` redeemer (loan-validator alt index 4, CBOR tag 125 / ``d87d``)
is reused byte-for-byte across the pool Spend, the loan Spend, and the pool Withdraw
hub.

The fixture embeds enough to (a) byte-exact test the redeemer + pool/loan datums and
(b) forward-replay against the spent inputs via Ogmios ``additionalUtxo``: full tx
CBOR, every resolved input (pool, loan, borrower) with address/value/inline datum,
reference inputs (incl. ref scripts), the decoded redeemers, the pool_in/pool_out +
loan_in/loan_out datums, and a ``realized`` block of computed deltas.

Run: PYTHONPATH=src python tests/lending/danogo/fixtures/_capture_increase_loan.py
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
from charli3_dendrite.lending.danogo.math import current_interest_index
from charli3_dendrite.lending.danogo.math import current_loan_amount
from charli3_dendrite.lending.math import bps_mul_ceil
from charli3_dendrite.lending.math import floor_div

# The Danogo Protocol Config NFT identifies the protocol-config reference UTxO whose
# datum yields the four script hashes used to classify the tx's roles.
PROTOCOL_CONFIG_NFT = (
    "bc2d3b7cf1009c788b1daf2208a38026ab20d7f8209d896ad1a09c14"
    "e567b64450c406a17f869eb4e430f2e2a6c74d103edb988cb1bc1347de285c69"
)

# The constructor index of IncreaseLoanAmount in the loan validator's redeemer union
# (alt index 4 == CBOR tag 125 == 0xd87d). The Withdraw(pool_skh) reward redeemer
# carrying it is the selector that distinguishes increase-loan txs from every other
# loan action (repay rides Withdraw(loan_skh) with tag 0xd87e).
INCREASE_LOAN_CONSTR_TAG = 125

# A chosen mainnet increase-loan transaction (USDA alt-supply market, cross-quote,
# packed oracle deployment). Overridable via env.
INCREASE_TX = os.environ.get(
    "INCREASE_LOAN_TX",
    "35a1ecf3a345ce54d8b009b77a6efa6cddb90cc0ec35214c3bda21db03b54d1a",
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


def _opt(tag_value):
    """Decode a Plutus Option: Some(x)==tag121[x] -> x; None==tag122[] -> None."""
    if isinstance(tag_value, cbor2.CBORTag):
        if tag_value.tag == 121:  # Some
            return tag_value.value[0]
        if tag_value.tag == 122:  # None
            return None
    raise ValueError(f"not an Option: {tag_value!r}")


def _decode_redeemer(cbor_hex: str) -> dict:
    """Decode an IncreaseLoanAmount redeemer (alt index 4) into its named fields.

    Mirrors the CreateLoan field shape, but ``loan_out_idx`` is a PLAIN ``int`` (the
    loan output is always present on an increase), not an ``Option<Int>``.
    """
    tag = cbor2.loads(bytes.fromhex(cbor_hex))
    assert (
        isinstance(tag, cbor2.CBORTag) and tag.tag == INCREASE_LOAN_CONSTR_TAG
    ), "expected IncreaseLoanAmount (alt index 4 / CBOR tag 125)"
    pool_out_idx, loan_out_idx, fee_out_idx, cfg_idx, market_idx, out_ref = tag.value
    assert isinstance(out_ref, cbor2.CBORTag) and out_ref.tag == 121
    tx_id, idx = out_ref.value
    return dict(
        constr=tag.tag - 121,
        pool_out_idx=pool_out_idx,
        loan_out_idx=loan_out_idx,
        fee_out_idx=_opt(fee_out_idx),
        protocol_cfg_ref_idx=cfg_idx,
        market_ref_idx=market_idx,
        pool_in_out_ref=[tx_id.hex(), idx],
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
    INPUT (a genuinely new collateral type that needs fresh oracle leaves downstream).
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


def _supply_qty(utxo: dict, supply_unit: str) -> int:
    """Quantity of the supply token (policy+name == ``supply_unit``) held by a UTxO."""
    policy, name = supply_unit[:56], supply_unit[56:]
    return next(
        (int(a[2]) for a in utxo["assets"] if a[0] == policy and a[1] == name),
        0,
    )


def _realized(
    *,
    pool_in: PoolDatum,
    pool_out: PoolDatum,
    loan_in: LoanDatum,
    loan_out: LoanDatum,
    market: DanogoMarket,
    pool_in_supply: int,
    pool_out_supply: int,
) -> dict:
    """Compute the on-chain increase-loan deltas (byte-exact targets for the builder).

    Increase-loan advances the pool exactly like create-loan (NOT repay): the accrued
    fee ACCUMULATES into ``undistributed_fee`` (it is not swept to a fee output) and
    the supply moves only by the accrued interest minus its fee plus any alt-token
    interest. The new borrow is booked as ``borrow_delta`` -- the rise in the loan's
    interest-accrued principal -- and the pool's raw supply-token holdings DROP by the
    amount paid out to the borrower (``pool_changed_amount < 0``).

    Observed behaviour (ties out exactly against the captured datums + values):

    - ``new_accumulated_interest = floor(total_borrow * (idx_out - idx_in) / idx_in)``.
    - ``new_loan_interest_fee = ceil(new_accumulated_interest * loan_fee_rate / bp)``.
    - ``current_loan_amount = floor(loan_amount * idx_out / loan.initial_index)``.
    - ``borrow_delta = loan_out.loan_amount - current_loan_amount`` (the increase, which
      includes any origination fee folded into the loan principal).
    - ``loan_origination_fee = pool_out.undistributed_fee - pool_in.undistributed_fee -
      new_loan_interest_fee`` (0 on current markets, whose origination rate is 0).
    - ``pool_out.total_borrow = total_borrow_in + new_accumulated_interest + borrow_delta``.
    - ``pool_out.undistributed_fee = undistributed_fee_in + new_loan_interest_fee +
      loan_origination_fee`` (accumulates -- the fee pot is NOT swept).
    - ``loan_out.loan_amount = current_loan_amount - pool_changed_amount +
      loan_origination_fee`` (increases, since ``pool_changed_amount < 0``).
    """
    new_accumulated_interest = floor_div(
        pool_in.total_borrow * (pool_out.interest_index - pool_in.interest_index),
        pool_in.interest_index,
    )
    new_loan_interest_fee = bps_mul_ceil(new_accumulated_interest, market.loan_fee_rate)
    current_loan = current_loan_amount(
        loan_amount=loan_in.loan_amount,
        current_index=pool_out.interest_index,
        initial_index=loan_in.initial_interest_index,
    )
    borrow_delta = loan_out.loan_amount - current_loan
    loan_origination_fee = (
        pool_out.undistributed_fee - pool_in.undistributed_fee - new_loan_interest_fee
    )
    pool_changed_amount = pool_out_supply - pool_in_supply
    alt_tokens_interest = (pool_out.total_supply - pool_in.total_supply) - (
        new_accumulated_interest - new_loan_interest_fee
    )

    # Cross-checks against the captured datum/value (raise if the model drifts).
    assert pool_out.interest_index == current_interest_index(
        pool_in.interest_index,
        borrow_apy=pool_in.borrow_apy,
        interest_time=pool_in.interest_time,
        txn_time=pool_out.interest_time,
    )
    assert pool_out.total_borrow == (
        pool_in.total_borrow + new_accumulated_interest + borrow_delta
    )
    assert pool_out.undistributed_fee == (
        pool_in.undistributed_fee + new_loan_interest_fee + loan_origination_fee
    )
    assert loan_out.loan_amount == (
        current_loan - pool_changed_amount + loan_origination_fee
    )
    assert loan_out.loan_amount > loan_in.loan_amount
    assert pool_changed_amount < 0  # pool pays out supply to the borrower
    return dict(
        txn_time=pool_out.interest_time,
        base_rate=market.base_rate,
        power_base=market.power_base,
        loan_fee_rate=market.loan_fee_rate,
        loan_origination_fee_rate=market.loan_origination_fee_rate,
        min_tx_amount=market.min_tx_amount,
        current_interest_index=pool_out.interest_index,
        new_accumulated_interest=new_accumulated_interest,
        new_loan_interest_fee=new_loan_interest_fee,
        current_loan_amount=current_loan,
        borrow_delta=borrow_delta,
        loan_origination_fee=loan_origination_fee,
        pool_changed_amount=pool_changed_amount,
        pool_supply_holdings_delta=pool_out_supply - pool_in_supply,
        alt_tokens_interest=alt_tokens_interest,
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

    # Spent inputs: this dbsync prunes consumed rows, so resolve via Blockfrost.
    inputs = _inputs_from_blockfrost(tx_hash, project_id)
    tx_cbor = _tx_cbor(tx_hash, project_id)

    # Derive the four script hashes from the protocol-config reference UTxO.
    cfg_policy, cfg_name = PROTOCOL_CONFIG_NFT[:56], PROTOCOL_CONFIG_NFT[56:]
    protocol_config = next(u for u in ref_inputs if _holds(u, cfg_policy, cfg_name))
    pd = ProtocolDatum.from_cbor(protocol_config["datum"])
    pool_skh = pd.pool_skh.hex()
    loan_skh = pd.loan_skh.hex()
    config_pool_skh = pd.config_pool_skh.hex()
    oracle_skh = pd.oracle_skh.hex()

    assert not mints, "an increase-loan tx mints nothing"

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

    # Loan input + output (both always present on an increase).
    loan_in = next(u for u in inputs if _parses(u["datum"], LoanDatum))
    loan_out_entry = next(u for u in outputs.values() if _parses(u["datum"], LoanDatum))

    # Market-param reference UTxO: holds the pool NFT but is not the pool itself.
    market = next(
        u
        for u in ref_inputs
        if _holds(u, config_pool_skh, market_name)
        and u["address"] != pool_in["address"]
    )
    market_info = DanogoMarket.from_market_datum(market["datum"])

    # The same IncreaseLoanAmount redeemer is reused across the pool Spend, the loan
    # Spend, and the Withdraw(pool_skh) hub; grab the hub (reward on pool_skh).
    increase_cbor = next(
        r["cbor"]
        for r in redeemers
        if r["purpose"] == "reward" and r["script_hash"] == pool_skh
    )
    oracle_cbor = next(
        (
            r["cbor"]
            for r in redeemers
            if r["purpose"] == "reward" and r["script_hash"] == oracle_skh
        ),
        None,
    )
    decoded = _decode_redeemer(increase_cbor)

    pool_in_datum_obj = PoolDatum.from_cbor(pool_in["datum"])
    pool_out_datum_obj = PoolDatum.from_cbor(pool_out_entry["datum"])
    loan_in_datum_obj = LoanDatum.from_cbor(loan_in["datum"])
    loan_out_datum_obj = LoanDatum.from_cbor(loan_out_entry["datum"])

    supply_token = loan_in_datum_obj.token_unit()
    # Collateral lives in the loan UTxO VALUE; compare loan_in vs loan_out holdings to
    # surface any collateral modification (out of v1 scope but captured for downstream).
    collateral = _collateral_delta(
        loan_in,
        loan_out_entry,
        loan_skh=loan_skh,
        market_name=market_name,
    )

    realized = _realized(
        pool_in=pool_in_datum_obj,
        pool_out=pool_out_datum_obj,
        loan_in=loan_in_datum_obj,
        loan_out=loan_out_datum_obj,
        market=market_info,
        pool_in_supply=_supply_qty(pool_in, supply_token),
        pool_out_supply=_supply_qty(pool_out_entry, supply_token),
    )
    realized["collateral"] = collateral

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
        mints=mints,
        redeemers=redeemers,
        inputs=inputs,
        ref_inputs=ref_inputs,
        outputs={str(k): v for k, v in outputs.items()},
        increase_loan_redeemer=increase_cbor,
        increase_loan_redeemer_decoded=decoded,
        oracle_redeemer=oracle_cbor,
        pool_in_out_ref=[pool_in["out_ref"][0], pool_in["out_ref"][1]],
        pool_in_datum=pool_in["datum"],
        pool_out_datum=pool_out_entry["datum"],
        loan_in_datum=loan_in["datum"],
        loan_out_datum=loan_out_entry["datum"],
        loan_in_amount=loan_in_datum_obj.loan_amount,
        loan_in_initial_index=loan_in_datum_obj.initial_interest_index,
        loan_out_amount=loan_out_datum_obj.loan_amount,
        loan_out_initial_index=loan_out_datum_obj.initial_interest_index,
        collateral=collateral,
        realized=realized,
        tx_cbor=tx_cbor,
    )


if __name__ == "__main__":
    here = Path(__file__).parent
    data = capture(INCREASE_TX)
    (here / "increase_loan_tx.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote increase_loan_tx.json (tx {data['tx_hash']})")
