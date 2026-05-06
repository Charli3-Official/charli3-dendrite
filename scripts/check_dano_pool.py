"""Validate Dano CLMM integration against a live mainnet pool.

Usage (from repo root):
    poetry run python scripts/check_dano_pool.py

Reads PROJECT_ID from .env, fetches the ADA/USDCx pool UTxO + protocol
config UTxO from Blockfrost, instantiates DanoCLMMState, and prints
parsed datum + a sample 100 ADA -> USDCx quote.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.dano import DanoCLMMState

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BLOCKFROST = "https://cardano-mainnet.blockfrost.io/api/v0"
POOL_ADDRESS = (
    "addr1x8vtd879xcmme7kmc3rfpqlhq67zj06dn53fvervtjsk0w7dwgsd23ac468cjj8rcnyuc"
    "3s72rtupu6j9dw0xpw83exsufvrg4"
)
PROTOCOL_CONFIG_OUT_REF = (
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


def _utxo_at(out_ref: str) -> dict:
    tx_hash, idx = out_ref.split("#")
    utxos = _bf(f"/txs/{tx_hash}/utxos")
    for o in utxos["outputs"]:
        if o["output_index"] == int(idx):
            return o
    raise RuntimeError(f"out_ref {out_ref} not found")


def _assets_from_amounts(amounts: list[dict]) -> Assets:
    return Assets(**{a["unit"]: int(a["quantity"]) for a in amounts})


def _platform_fee_rate() -> int:
    """Read platform_fee_rate from the protocol-config UTxO datum."""
    out = _utxo_at(PROTOCOL_CONFIG_OUT_REF)
    # ProtocolConfigDatum = Constr 0 [platformFeeRate, swapFee]
    # inline_datum CBOR — parse with pycardano RawPlutusData.
    from pycardano import RawPlutusData

    raw = RawPlutusData.from_cbor(out["inline_datum"])
    fields = raw.data.value  # CBORTag(121, [...])
    return int(fields[0])


def main() -> None:
    pool_utxos = _bf(f"/addresses/{POOL_ADDRESS}/utxos?count=20")
    if not pool_utxos:
        raise SystemExit("No UTxOs at pool address.")
    utxo = pool_utxos[0]

    block = _bf(f"/blocks/{utxo['block']}")

    state = DanoCLMMState(
        assets=_assets_from_amounts(utxo["amount"]),
        block_time=block["time"],
        block_index=block["tx_count"],
        tx_hash=utxo["tx_hash"],
        tx_index=utxo["output_index"],
        datum_hash=utxo["data_hash"],
        datum_cbor=utxo["inline_datum"],
        plutus_v2=True,
    )
    state.platform_fee_rate = _platform_fee_rate()

    d = state.pool_datum
    print("=== Dano pool ===")
    print(f"  pool_id          : {state.pool_id}")
    print(f"  pair             : {state.unit_a} / {state.unit_b}")
    print(f"  reserves (raw)   : {state.assets.quantity(0):,} | "
          f"{state.assets.quantity(1):,}")
    print(f"  reserves (active): {state.reserve_a:,} | {state.reserve_b:,}")
    print(f"  lp_fee_rate (bps): {d.lp_fee_rate}")
    print(f"  platform_fee_x   : {d.platform_fee_x:,}")
    print(f"  platform_fee_y   : {d.platform_fee_y:,}")
    print(f"  total_swap_fee   : {d.total_swap_fee:,}")
    print(f"  sqrt_lower       : {d.sqrt_lower_price.numerator}"
          f"/{d.sqrt_lower_price.denominator}")
    print(f"  sqrt_upper       : {d.sqrt_upper_price.numerator}"
          f"/{d.sqrt_upper_price.denominator}")
    print(f"  platform_fee_rate (from protocol config): {state.platform_fee_rate}")

    # state.price returns (A per B, B per A)
    ada_per_usdcx, usdcx_per_ada = state.price
    print(f"\n  spot price       : 1 ADA ~ {usdcx_per_ada:.6f} USDCx")
    print(f"                     1 USDCx ~ {ada_per_usdcx:.6f} ADA")

    for ada in (1, 100, 10_000):
        qty = ada * 1_000_000
        try:
            out, impact = state.get_amount_out(Assets(lovelace=qty))
            usdcx = out.quantity() / 1_000_000
            print(f"  swap {ada:>6} ADA -> {usdcx:,.6f} USDCx  "
                  f"(price impact {impact:+.4%})")
        except Exception as e:  # noqa: BLE001
            print(f"  swap {ada} ADA failed: {e}")


if __name__ == "__main__":
    main()
