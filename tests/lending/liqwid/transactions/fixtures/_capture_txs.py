"""Dev-only: capture Liqwid V2 user transactions from dbsync into replay fixtures.

Writes two gzipped JSON files next to this script (``mtime=0``, so re-capturing
identical data is byte-identical), merging into what is already there:

``txs.json.gz``: ``{label: capture}``. Each capture has the keys of the FluidTokens
V4 fixtures (``label, tx_hash, block_time, fee, invalid_before, invalid_hereafter,
inputs, outputs, ref_inputs, mints, redeemers``) plus ``required_signers`` (key
hashes, hex, sorted) and ``withdrawals`` (``[credential hash hex, amount]``). Every
UTxO is ``{address, lovelace, assets, datum, ref_script, ref_script_type, out_ref}``,
with ``datum_hash`` added only when the output carries a datum hash and no inline
datum. ``ref_script`` is always null here: the bytes live in ``scripts.json.gz``.
Inputs and reference inputs are sorted by out-ref, as the ledger orders them.

``scripts.json.gz``: ``{"<tx hash>#<index>": {ref_script, ref_script_type}}``, every
reference script the captures hold, stored once.

Run (read-only against dbsync):

    DBSYNC_DSN="postgresql://..." PYTHONPATH=src:. \\
        python tests/lending/liqwid/transactions/fixtures/_capture_txs.py [label ...]

Labels limit the run to those jobs; without labels every job is captured. Without
``DBSYNC_DSN`` the five ``DBSYNC_*`` variables are read. Not run in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import psycopg

from tests.lending.fluidtokens.transactions.fixtures._capture_loan_action import (
    _utxo as _plain_utxo,
)
from tests.lending.liqwid.transactions.replay import SCRIPTS_FILE
from tests.lending.liqwid.transactions.replay import TXS_FILE
from tests.lending.liqwid.transactions.replay import dbsync_connect_kwargs
from tests.lending.liqwid.transactions.replay import detach_scripts
from tests.lending.liqwid.transactions.replay import read_json_gz
from tests.lending.liqwid.transactions.replay import write_json_gz

HERE = Path(__file__).parent

JOBS = {
    # Supply and redeem; redeem_djed_floor pays out exactly floor(Q * s9).
    "supply_ada": "e4cee5026b3995af2d813f20c2f4c934f5f08353c13d9424d13de9ef91f84509",
    "supply_djed": "4d996b21f83c4a990f843e3e0e0a5e3ddf3002f6e98e662e66eccd92f49ae630",
    "redeem_ada": "b6967e75c3e0d08d54f7690d4b3edc82c2fd3d78e2cccdbffb84f1ee538a18d7",
    "redeem_djed_floor": "7af6ae8a596221df24f136840549bf979363700c94d65940c89d626b71cb6936",
    # Open a loan: one collateral, three, the liquidation staking role first in
    # p[34], a borrow at the limit, and an indefinite-datum loan.
    "open_single": "574f8f14505eb51cd34fc96f769d81da4c57d96a53fb789871b65e10c8b4297d",
    "open_three": "10d3115fe3ec848e088e73600def0cf2d5586e6eabe30cc612c4043fc8c31f42",
    "open_liquidation_first": "a6136c818f6cd6cdc130b70c6a762c42ba3e544241a98066677b7aab68e652c9",
    "open_at_limit": "3282f9bbd794dea4fc848cfe3b8ef6e1099344605f504ae7b18abc9ab2d854ef",
    "open_indefinite": "ef65a01b49eecf1639c188dce49fa779e9be491aceb0155ab5170f30c35be19f",
    # Borrow more, alone and with a collateral change.
    "increase_ada": "2a2930b0ef53cb7f4b5f9705cd28889e576559c0fc798224393666e9ecbc6063",
    "increase_usdm": "efac5202df8509aa07b7a91f9d709dc34295eeaaa561588f887dfbc396506cc1",
    "increase_with_collateral": "e9122e77c6d86187f34d9924382ce69e6973b91c52aa9bcfffbf93bba7b3b566",
    # Partial repay: plain, the clamped debt, interest only, with a collateral
    # change.
    "repay_usdm": "89f443ffb50e53eaa3cdd4b376f5e37fafd4211b3ae8e9ca402feaea700ded8d",
    "repay_clamp": "1478680fb708a213a7c52cda8eefa81221a3e66c74a6f6b5abdac65403930e16",
    "repay_interest_only": "6d7eff5a541cc531f9ae79580bab9f49d36f67b69cc4d835d58c3600c353c9cd",
    "repay_with_collateral": "5a11bc568ee41c704ac7a1d61a0e33e66f6a55db61379bc1c749c1da396b3763",
    # Close: plain, with a top-up, and a zero-debt loan.
    "close_ada": "9362b410197dc6b00912733972342fbb5ee016df70949b768ed92d9e7c49df20",
    "close_top_up": "f368e1b2d982947a707f4833ce008ebcc0857cf34e90db7de59d2a754c7126b9",
    "close_zero_debt": "bbf9d17a6ba5e9bdd46cffcb0a8dff96a748469f1c317dac28aa1b95aac8b200",
    # Collateral changes.
    "collateral_add": "e918ce4b7b37f37b622c86fbac177206f755946650396819d2a5db9ec75663d8",
    "collateral_multi_add": "254548716d98ed9f84ec3e1ea04fa961354021139fdbd85733949b6c04dabbe7",
    "collateral_five": "650d40277f8464dfe6214e30b3311ac2738c53c13e90255a6745e8563a62b7b8",
    "collateral_remove": "fade9918433a64cfdfd9f7d7ac3f6e048d790e5cb930515e0cfcc1f966aa4868",
    "collateral_remove_two": "ba49681c6fc359b69c0e7fbfd394076470bda1023caa317f48bb7e1c00c94b8c",
    "collateral_drop_policy": "cf6def5184240d2eb57752512ec93526f89f9bf496cf9966a20b44e274825053",
    "collateral_rebase": "587ee4cff107469054046ba92e2b688b6e7a212e01a561f8562d176a23fa55f7",
    # Combinations.
    "supply_and_borrow_ada_night": "d2d1de2dbe4bdbeed5360d0728833be5a9ff9b44678b0d8a8f254d546c6075a4",
    "supply_and_borrow_usdt_usdc": "35a0b20327f4ef3b338d0b6b0bb2dc6f36567d05f2dd5710529abb9b1f2b60e6",
    "remove_and_redeem_a": "37f1f11fefeb60cd2d39d29d131c6bedbef728288e32372fc6680ec4a38c3f6b",
    "remove_and_redeem_night": "8bdf83ec3a4e0beffee2229c3b5589365f616e77d6dd859f71aec6591c392a57",
    # One flow per market family: isolated, collateral-only, LP-token, staked.
    "family_isolated_supply": "2bf96ad7da7270197799bf740e7f42960988c6d542962b8f62a44a64548be95a",
    "family_isolated_redeem": "1c965d8ad4693fa2b196268e6f811f4f035df9e8fdb644cd6cca291a6a838cd1",
    "family_isolated_open": "cb563b6ef0fea962f3b1d0c69fdd67220bb516cab9a3b8322bdf220b34a02c97",
    "family_isolated_repay": "ae44af0bb2dcf08ac0b399abe55f26ba11d8ae35ef847d4d06db3d20e3b9454e",
    "family_isolated_close": "23f99dd5dbb4f50c57b99aef460cdf494d1627b8f9091ab481e2e9b8e79f763b",
    "family_isolated_collateral": "106c9794f85f64d437a9b3e2ae79bb9d1a4883424dbcc49d2255b61e0dbe1849",
    "family_collateral_only_supply": "b30bd911edf747beb19cf37afeb09380f9a545e2a800bbb194f255559ea76416",
    "family_lp_redeem": "3f7f3159ebfc9e15b083b081a290b8e4149e416df46243b2ad8d76a845a5f640",
    "family_lp_increase": "1714ba95b74ba6e9ab0090ac6ecaf2f53a4d3281f294198cd53ba54fc3692bc9",
    "family_lp_repay": "0abaea2bff1877e48bd7d8e47430f79546b8338088daf323e8bdd4508c5afb4b",
    "family_lp_close": "6e7a7e5b4f5a42a1b2ba4383ed0332cbdf9b99a2e4e0cfbd3e4865d1ca778832",
    "family_lp_collateral": "e6982f4340b09f055c5c3cd4e8a00efea4dc57d6893628cc79614cb0896fa262",
    "family_staked_supply": "ecec23a1f2f721b41e1fd5233b4a5b9ec42afff14a4428b7c41bfb480562b569",
    "family_staked_increase": "26a85ab3bed4b1c1e08d783fab0f85568fc0ddf76bf55c166a99678616ca1f9e",
    "family_staked_close": "d73bb2bcb008862ed1c9d82107ca8db66c3e7b5aa3ad8e79c15ff3dce282c5a5",
}


def _utxo(cur: Any, tx_out_id: int) -> dict[str, Any]:
    """A captured UTxO, with its datum hash when it has no inline datum."""
    utxo = _plain_utxo(cur, tx_out_id)
    cur.execute(
        """SELECT encode(o.data_hash,'hex') FROM tx_out o
           WHERE o.id=%s AND o.inline_datum_id IS NULL""",
        (tx_out_id,),
    )
    row = cur.fetchone()
    if row is not None and row[0] is not None:
        utxo["datum_hash"] = row[0]
    return utxo


def _by_out_ref(utxos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(utxos, key=lambda u: (u["out_ref"][0], u["out_ref"][1]))


def capture(cur: Any, tx_hash: str, *, label: str) -> dict[str, Any]:
    """The capture of the transaction ``tx_hash``, reference scripts inline."""
    cur.execute(
        """SELECT t.id, t.invalid_before, t.invalid_hereafter, t.fee, b.time
           FROM tx t JOIN block b ON b.id=t.block_id
           WHERE t.hash=decode(%s,'hex')""",
        (tx_hash,),
    )
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"{label}: transaction {tx_hash} is not in dbsync")
    tid, inv_before, inv_after, fee, block_time = row

    cur.execute(
        "SELECT id FROM tx_out WHERE consumed_by_tx_id=%s ORDER BY id",
        (tid,),
    )
    inputs = _by_out_ref([_utxo(cur, r[0]) for r in cur.fetchall()])

    cur.execute("SELECT id FROM tx_out WHERE tx_id=%s ORDER BY index", (tid,))
    outputs = [_utxo(cur, r[0]) for r in cur.fetchall()]

    cur.execute(
        """SELECT o.id FROM reference_tx_in r
           JOIN tx_out o ON o.tx_id=r.tx_out_id AND o.index=r.tx_out_index
           WHERE r.tx_in_id=%s""",
        (tid,),
    )
    ref_inputs = _by_out_ref([_utxo(cur, r[0]) for r in cur.fetchall()])

    cur.execute(
        """SELECT encode(ma.policy,'hex'), encode(ma.name,'hex'), m.quantity
           FROM ma_tx_mint m JOIN multi_asset ma ON ma.id=m.ident WHERE m.tx_id=%s
           ORDER BY ma.policy, ma.name""",
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
        {"purpose": pu, "script_hash": sh, "index": int(ix), "cbor": cb}
        for pu, sh, ix, cb in cur.fetchall()
    ]

    cur.execute(
        """SELECT encode(w.hash,'hex') FROM extra_key_witness w
           WHERE w.tx_id=%s ORDER BY 1""",
        (tid,),
    )
    required_signers = [r[0] for r in cur.fetchall()]

    # The credential is the reward address without its header byte, so key-hash
    # withdrawals read the same way as script withdrawals.
    cur.execute(
        """SELECT encode(substring(sa.hash_raw from 2),'hex'), w.amount
           FROM withdrawal w JOIN stake_address sa ON sa.id=w.addr_id
           WHERE w.tx_id=%s ORDER BY 1""",
        (tid,),
    )
    withdrawals = [[h, int(amount)] for h, amount in cur.fetchall()]

    return {
        "label": label,
        "tx_hash": tx_hash,
        "block_time": (
            int(block_time.timestamp())
            if hasattr(block_time, "timestamp")
            else int(block_time)
        ),
        "fee": int(fee),
        "invalid_before": int(inv_before) if inv_before is not None else None,
        "invalid_hereafter": int(inv_after) if inv_after is not None else None,
        "inputs": inputs,
        "outputs": outputs,
        "ref_inputs": ref_inputs,
        "mints": mints,
        "redeemers": redeemers,
        "required_signers": required_signers,
        "withdrawals": withdrawals,
    }


def main(argv: list[str]) -> None:
    """Capture the jobs named in ``argv`` (every job without names)."""
    unknown = sorted(set(argv) - set(JOBS))
    if unknown:
        raise SystemExit(f"unknown labels: {', '.join(unknown)}")
    chosen = argv or list(JOBS)
    txs = read_json_gz(HERE / TXS_FILE)
    scripts = read_json_gz(HERE / SCRIPTS_FILE)
    try:
        with psycopg.connect(**dbsync_connect_kwargs()) as conn, conn.cursor() as cur:
            for label in chosen:
                fix = capture(cur, JOBS[label], label=label)
                scripts.update(detach_scripts(fix))
                txs[label] = fix
                print(
                    f"wrote {label} (tx {JOBS[label][:12]}, "
                    f"{len(fix['inputs'])} inputs, {len(fix['outputs'])} outputs, "
                    f"{len(fix['ref_inputs'])} refs)",
                )
    finally:
        # Keep what was captured before a failure; a re-run merges over it.
        write_json_gz(HERE / TXS_FILE, txs)
        write_json_gz(HERE / SCRIPTS_FILE, scripts)


if __name__ == "__main__":
    main(sys.argv[1:])
