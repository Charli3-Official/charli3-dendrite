"""Dev-only: capture real FluidTokens V4 borrower and lender txs into JSON fixtures.

Reuses the V3 capture (same JSON schema: validity window, spent inputs, outputs,
canonically-sorted reference inputs with their scripts, mints and every redeemer),
recognising loans with the V4 ``LoanDatum``. One file per job, named after its label.
``reference_scripts.json`` holds the reference-script UTxOs of the scripts no captured
transaction runs yet (the recast action and the repayment-receipt policy).

Run: PYTHONPATH=src:. python tests/lending/fluidtokens_v4/transactions/fixtures/_capture_loan_action.py
Requires dbsync env (DBSYNC_*). Not run in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg

from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from tests.lending.fluidtokens.transactions.fixtures._capture_loan_action import CONN
from tests.lending.fluidtokens.transactions.fixtures._capture_loan_action import _utxo
from tests.lending.fluidtokens.transactions.fixtures._capture_loan_action import capture

JOBS = {
    # One pool, ADA principal, STRIKE collateral priced by one signed oracle reward.
    "borrow_single": "6f5164c446c943d689e2a3257f22ef665451c86424868970adb908e89972ecf3",
    # Six pools in one tx, all ADA principal against IAG collateral, one oracle reward.
    "borrow_multi": "e845b2d7cafc6e59f48b3cd430d55fb6d99f3243409f72c0266f30f6f7cb57fd",
    # Final repay of one perpetual loan; the loan NFT burns, the lender is paid at the
    # asset manager and the borrower bond returns to the wallet.
    "repay_single": "6e48c3d8f5fcdbe8f3fb06d1d2efd8652410ce3855a52f8b55c97ac44fb155c7",
    # Final repay of three perpetual loans in one tx.
    "repay_multi": "c0778713fb7c643a2e7a44f5a948b1edb3ea585ad0a11d2b1d60a264e5abe616",
    # Collateral added to one oracle-priced loan.
    "change_collateral_single": (
        "e5fc29bb45627db2223671e1795e9da1ac58a4deb1e7a0cab09e742cc4f7306b"
    ),
    # Collateral changed on three loans in one tx.
    "change_collateral_multi": (
        "cfd68920417356bc3005a3471b39b09bb85f2971447de673d322c4a2ee4d9a17"
    ),
    # A pool lending ADA against STRIKE, created from twelve wallet inputs; the pool
    # names derive from an input that is not the first.
    "pool_create": "16d4066f4489bb4bcc987983c2d748bdc48b92257f3d3bc5127931ba9b494f08",
    # A pool lending USDCx.
    "pool_create_token": (
        "315e5101d7e7cbe5f800de7fcf6807d34236318d8adaa22621a69b3dfa7310a3"
    ),
    # Two collateral options appended to a pool lending USDM.
    "pool_edit": "6e3b3af7fc7e5d5c0c9057ed296be6d1c9bcc2e0bcf93704b3a7bb26e36f61d8",
    # 10 ADA added to a pool lending ADA, with two collateral options appended.
    "pool_edit_deposit": (
        "f42535b4fc9d844857c4a4596d44f583571c5279f9a295ec864165930e4bb959"
    ),
    # A pool lending ADA cancelled; its pool manager sorts before the pool.
    "pool_cancel": "a39195b6bc6f4d07d2c06b8c021600fe4512549c0d80cd12f7832e0e80eda684",
    # A lender claims one repayment: the lender bond leaves the lender manager and
    # returns unchanged, and the repayment pays the fee and goes to the lender.
    "claim": "6f0cbf4d49616c72c3430c40f5695aa4b5592ceac54153463a219fe1506678eb",
    # The same claim with the lender-bond input sorting before the repayment.
    "claim_bond_first": (
        "9cdc439f0258e719522c81253a4a6cd5c36bd513ea72b7d8a24ff7ba9771e35c"
    ),
}

REFERENCE_SCRIPTS = {
    "recast_action": (
        "ef0f25ea280d44db49a3916ba264ba533751d2718656dbe8457cb2d5471546e8",
        0,
    ),
    "repayment_policy": (
        "bc9dcf69d9093efc98ec3b7c31506b18b61074286c8d9a479443a98a1d4f20f0",
        0,
    ),
}


def capture_reference_scripts() -> dict[str, dict]:
    """Each :data:`REFERENCE_SCRIPTS` UTxO in the fixture schema, by label."""
    with psycopg.connect(**CONN) as conn, conn.cursor() as cur:
        out = {}
        for label, (tx_hash, index) in REFERENCE_SCRIPTS.items():
            cur.execute(
                """SELECT o.id FROM tx_out o JOIN tx t ON t.id = o.tx_id
                   WHERE t.hash = decode(%s, 'hex') AND o.index = %s""",
                (tx_hash, index),
            )
            out[label] = _utxo(cur, cur.fetchone()[0])
        return out


if __name__ == "__main__":
    here = Path(__file__).parent
    (here / "reference_scripts.json").write_text(
        json.dumps(capture_reference_scripts(), indent=2, sort_keys=True) + "\n",
    )
    for label, tx in JOBS.items():
        data = capture(tx, label=label, loan_datum_cls=LoanDatum)
        (here / f"{label}.json").write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
        )
        print(
            f"wrote {label}.json (tx {tx[:12]}, {len(data['inputs'])} inputs, "
            f"{len(data['outputs'])} outputs, {len(data['ref_inputs'])} refs, "
            f"mints={len(data['mints'])})",
        )
