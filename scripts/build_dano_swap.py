"""Dry-run: reconstruct mainnet Dano swap tx(s) from spec-driven builders
and diff each pool against the on-chain values.

Supports both single-pool and multi-pool batched swaps. For each pool input
we verify:
  * compute_pool_change()   — matches observed pool reserve deltas
  * compute_new_datum()     — matches observed inline datum byte-for-byte
  * redeemer entry          — matches the corresponding pool entry in
                              the spend redeemer payload

Usage: python scripts/build_dano_swap.py [tx_hash]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.dano import DANO_POOL_SCRIPT_HASH_MAINNET
from charli3_dendrite.dexs.amm.dano import DanoCLMMState
from charli3_dendrite.dexs.amm.dano import build_batch_swap_redeemer_bytes
from charli3_dendrite.dexs.amm.dano import parse_swap_redeemer_bytes

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BLOCKFROST = "https://cardano-mainnet.blockfrost.io/api/v0"
DEFAULT_TX = "716ce79699f1a51e9eaa7196bf21cdcc2522d565d354da9e702ebc2fb36435b4"
PROTOCOL_CONFIG_OUTREF = (
    "2cafd7c92f7093e5229af274be83dea660b0590b4174bbed79ba662b44fbd1ee#0"
)


def _bf(path: str) -> dict | list:
    r = requests.get(
        f"{BLOCKFROST}{path}",
        headers={"project_id": os.environ["PROJECT_ID"]},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _platform_fee_rate(datum_cbor: str) -> int:
    from pycardano import RawPlutusData

    return int(RawPlutusData.from_cbor(datum_cbor).data.value[0])


def _swap_fee(datum_cbor: str) -> int:
    from pycardano import RawPlutusData

    return int(RawPlutusData.from_cbor(datum_cbor).data.value[1])


def _assets_from_amounts(amounts: list[dict]) -> Assets:
    return Assets(**{a["unit"]: int(a["quantity"]) for a in amounts})


def _has_validity_nft(amounts: list[dict]) -> bool:
    return any(
        a["unit"].startswith(DANO_POOL_SCRIPT_HASH_MAINNET) and a["unit"] != ""
        for a in amounts
    )


def _strip_cbor_bytes_header(hex_str: str) -> bytes:
    """Strip the leading CBOR byte-string header to recover the raw payload.

    Handles definite (0x40..0x5b) and indefinite-length (0x5f...0xff) bytes.
    """
    import cbor2

    return cbor2.loads(bytes.fromhex(hex_str))


def _redeemer_payload(data_hash: str) -> bytes:
    cbor_hex = _bf(f"/scripts/datum/{data_hash}/cbor")["cbor"]
    return _strip_cbor_bytes_header(cbor_hex)


def _ok(b: bool) -> str:
    return "OK" if b else "DIFF"


def main() -> None:
    tx_to_match = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TX
    print(f"Verifying against tx {tx_to_match}\n")

    tx = _bf(f"/txs/{tx_to_match}/utxos")
    redeemers = _bf(f"/txs/{tx_to_match}/redeemers")

    # Block timestamp -> curEpoch (mainnet)
    pool_inputs = [
        i for i in tx["inputs"]
        if not i["reference"] and _has_validity_nft(i["amount"])
    ]
    pool_outputs = [o for o in tx["outputs"] if _has_validity_nft(o["amount"])]
    print(f"Pool inputs:  {len(pool_inputs)}")
    print(f"Pool outputs: {len(pool_outputs)}")

    first_pool_tx_hash = pool_inputs[0]["tx_hash"]
    first_pool_tx = _bf(f"/txs/{first_pool_tx_hash}")
    first_pool_block = _bf(f"/blocks/{first_pool_tx['block']}")

    # Protocol config
    pc_input = next(
        i for i in tx["inputs"]
        if i["reference"]
        and i["tx_hash"] == PROTOCOL_CONFIG_OUTREF.split("#")[0]
    )
    platform_fee_rate = _platform_fee_rate(pc_input["inline_datum"])
    swap_fee = _swap_fee(pc_input["inline_datum"])
    print(f"Protocol config: platform_fee_rate={platform_fee_rate}, "
          f"swap_fee={swap_fee}\n")

    # Sorted spend-input order so we can compute pool_in_idx values
    spend_inputs_sorted = sorted(
        [(i["tx_hash"], i["output_index"]) for i in tx["inputs"]
         if not i["reference"]],
    )

    # Reward redeemer payload (shared across all pools in a batch)
    reward_redeemer = next(
        (r for r in redeemers if r["purpose"] == "reward"), None,
    )
    reward_payload = (
        _redeemer_payload(reward_redeemer["redeemer_data_hash"])
        if reward_redeemer else None
    )

    spend_redeemers = sorted(
        [r for r in redeemers if r["purpose"] == "spend"],
        key=lambda r: r["tx_index"],
    )

    # Match each pool input to its spend redeemer by pool_in_idx (byte 0
    # of the spend payload is the pool_in_idx of THIS spend's pool).
    pool_in_idxs = {
        idx: spend_inputs_sorted.index((p["tx_hash"], p["output_index"]))
        for idx, p in enumerate(pool_inputs)
    }

    # Reverse lookup: pool_in_idx -> pool_input
    pool_in_by_idx = {
        spend_inputs_sorted.index((p["tx_hash"], p["output_index"])): p
        for p in pool_inputs
    }

    # Build the per-pool redeemer entries from one of the spend redeemers
    # (they all share the same per-pool tuple list — only byte 0 differs).
    sample_payload = _redeemer_payload(spend_redeemers[0]["redeemer_data_hash"])
    _, _, entries = parse_swap_redeemer_bytes(sample_payload)
    print(f"Redeemer entries (n={len(entries)}):")
    for e in entries:
        print(f"   pool_in_idx={e[0]}, pool_out_idx={e[1]}, "
              f"delta_amount={e[2]:+,}")
    print()

    all_ok = True

    for entry_idx, (entry_in_idx, entry_out_idx, delta_amount) in enumerate(entries):
        print(f"=== Pool entry {entry_idx + 1}/{len(entries)} "
              f"(pool_in_idx={entry_in_idx}, pool_out_idx={entry_out_idx}) ===")

        pool_in = pool_in_by_idx[entry_in_idx]
        pool_address = pool_in["address"]
        observed_new_pool = tx["outputs"][entry_out_idx]
        if observed_new_pool["address"] != pool_address:
            print(f"  WARN: output {entry_out_idx} address {observed_new_pool['address']} "
                  f"!= pool address {pool_address}")
        observed_new_datum = observed_new_pool["inline_datum"]

        # Build pool state (we use the first pool block's time for cur_epoch
        # — it's the same tx so all pools share validity range).
        state = DanoCLMMState(
            assets=_assets_from_amounts(pool_in["amount"]),
            block_time=first_pool_block["time"],
            block_index=first_pool_block["tx_count"],
            tx_hash=pool_in["tx_hash"],
            tx_index=pool_in["output_index"],
            datum_hash=pool_in["data_hash"],
            datum_cbor=pool_in["inline_datum"],
            plutus_v2=True,
        )
        state.platform_fee_rate = platform_fee_rate
        d = state._datum

        pc_x, pc_y = state.compute_pool_change(delta_amount)
        new_pool_assets = {
            a["unit"]: int(a["quantity"]) for a in observed_new_pool["amount"]
        }
        obs_x = new_pool_assets.get(d.unit_x, 0) - state.raw_x
        obs_y = new_pool_assets.get(d.unit_y, 0) - state.raw_y
        expected_x_with_fee = pc_x + (swap_fee if d.unit_x == "lovelace" else 0)
        x_ok = expected_x_with_fee == obs_x
        y_ok = pc_y == obs_y
        all_ok &= x_ok and y_ok
        ada_note = ("  (+swap_fee for ADA pool)" if d.unit_x == "lovelace" else "")
        print(f"  X change: computed={pc_x:+,}  observed={obs_x:+,}  "
              f"{_ok(x_ok)}{ada_note}")
        print(f"  Y change: computed={pc_y:+,}  observed={obs_y:+,}  {_ok(y_ok)}")

        t_ms = first_pool_block["time"] * 1000
        cur_epoch = (t_ms - 1_647_899_091_000) // 432_000_000 + 328
        new_datum = state.compute_new_datum(
            delta_amount=delta_amount,
            swap_fee=swap_fee,
            cur_epoch=cur_epoch,
        )
        d_ok = new_datum.to_cbor_hex() == observed_new_datum
        all_ok &= d_ok
        print(f"  datum CBOR: {_ok(d_ok)}")
        if not d_ok:
            print(f"    computed: {new_datum.to_cbor_hex()}")
            print(f"    observed: {observed_new_datum}")
        print()

    # Verify each spend redeemer's payload reproduces from our builder.
    print("=== spend redeemer payloads ===")
    for sr in spend_redeemers:
        observed = _redeemer_payload(sr["redeemer_data_hash"])
        first_byte = observed[0]
        computed = build_batch_swap_redeemer_bytes(
            first_byte=first_byte,
            entries=entries,
        )
        match = computed == observed
        all_ok &= match
        print(f"  spend tx_index={sr['tx_index']} (first_byte={first_byte}): "
              f"{_ok(match)}")
        if not match:
            print(f"    computed: {computed.hex()}")
            print(f"    observed: {observed.hex()}")

    if reward_payload is not None:
        first_byte = reward_payload[0]
        computed = build_batch_swap_redeemer_bytes(
            first_byte=first_byte,
            entries=entries,
        )
        match = computed == reward_payload
        all_ok &= match
        print(f"  reward (first_byte=protocol_config_idx={first_byte}): {_ok(match)}")
        if not match:
            print(f"    computed: {computed.hex()}")
            print(f"    observed: {reward_payload.hex()}")

    print()
    print(f"OVERALL: {'ALL OK' if all_ok else 'FAILED'}")


if __name__ == "__main__":
    main()
