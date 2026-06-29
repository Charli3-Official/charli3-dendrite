"""Gated end-to-end: a forward-built modify-collateral evaluates on Ogmios.

The decisive correctness proof for the modify-collateral (ModifyCollaterals) builder. It
is self-contained: rather than resolving live pool/loan state (the captured loan is
already spent), it reconstructs a `ModifyCollateralSnapshot` from a captured real
on-chain modify, forward-builds a brand-new (unsigned, unsubmitted) modify against the
captured spent inputs, and asks the real Ogmios to execute every Plutus script via
``evaluateTransaction`` -- feeding the spent inputs + reference UTxOs back through
``additionalUtxo`` (spent/historical UTxOs supplied that way are stable for evaluation,
independent of any later on-chain reference-script rotation).

A modify returns TWO positive execution budgets: the loan ``Spend`` (``ModifyCollaterals``)
and the oracle ``Withdraw`` price calc. It mints NOTHING and spends NO pool (the pool is
a reference input). The validity window is pinned to the captured transaction's so the
validator's interest accrual matches. Nothing is signed or submitted.

All three fixtures FORWARD-synthesize their oracle ``Withdraw`` from live source leaves
-- no captured redeemer -- proving the forward pricer reproduces the on-chain price calc
(prices + referenced leaf set) across both deployments. REMOVE is the packed deployment;
SWAP and ADD are the structured deployment. SWAP and ADD exercise the structured
deployment's multi-path pricing: each priced unit's primary derivation path is composed
forward while its alternative deviation cross-check paths are referenced (the same shared
``_structured_*`` machinery in ``transactions/_common`` repay/increase use). ADD in
particular resolves a Liqwid market-state source (rate-bearing STATE leaf + companion
PARAM leaf) and an Orcfax source (rate-bearing FS leaf + FSP pointer leaf), and its
``lovelace`` intermediate price is the minimum across its resolving deviation paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from typing import Callable

import pytest
from dotenv import load_dotenv

load_dotenv()
from pycardano import Address  # noqa: E402
from pycardano import TransactionBuilder  # noqa: E402

from charli3_dendrite.lending.danogo.transactions.build import (  # noqa: E402
    add_modify_collateral_funding,
)
from charli3_dendrite.lending.danogo.transactions.build import (  # noqa: E402
    build_modify_collateral,
)
from charli3_dendrite.lending.danogo.transactions.build import (  # noqa: E402
    safe_collateral_for_modify,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = [
    "modify_collateral_add_tx.json",
    "modify_collateral_remove_tx.json",
    "modify_collateral_swap_tx.json",
]

# Every modify fixture FORWARD-synthesizes its oracle ``Withdraw`` from live source leaves
# (no captured redeemer): the packed-deployment REMOVE market and the structured-deployment
# SWAP + ADD markets (multi-path, single-leaf sources across the Liqwid / Danogo-pool /
# Indigo / Orcfax / Liqwid-market source kinds).
FORWARD_FIXTURES = frozenset(FIXTURES)

# Ogmios `additionalUtxo` reference-script language tags, keyed by db-sync's Plutus
# version label (all the Danogo reference scripts are PlutusV3).
_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _utxo_entry(u: dict[str, Any]) -> dict[str, Any]:
    """An Ogmios `additionalUtxo` entry resolving one captured UTxO."""
    value: dict[str, Any] = {"ada": {"lovelace": int(u["lovelace"])}}
    for policy, asset_name, qty in u["assets"]:
        value.setdefault(policy, {})[asset_name] = int(qty)
    entry: dict[str, Any] = {
        "transaction": {"id": u["out_ref"][0]},
        "index": u["out_ref"][1],
        "address": u["address"],
        "value": value,
    }
    if u.get("datum"):
        entry["datum"] = u["datum"]
    if u.get("ref_script"):
        lang = _SCRIPT_LANG.get(u.get("ref_script_type") or "", "plutus:v2")
        entry["script"] = {"language": lang, "cbor": u["ref_script"]}
    return entry


def _additional_utxo(fix: dict[str, Any]) -> list[dict[str, Any]]:
    """The captured spent inputs + reference inputs as the Ogmios `additionalUtxo` set."""
    return [_utxo_entry(u) for u in fix["inputs"] + fix["ref_inputs"]]


def _borrower(
    fix: dict[str, Any], snapshot: ModifyCollateralSnapshot
) -> tuple[Address, str]:
    """The captured borrower (funding) input's address + out-ref: it holds the owner NFT."""
    policy, asset_name = snapshot.owner_nft[:56], snapshot.owner_nft[56:]
    utxo = next(
        u
        for u in fix["inputs"]
        if any(
            p == policy and n == asset_name and int(q) == 1 for p, n, q in u["assets"]
        )
    )
    out_ref = f"{utxo['out_ref'][0]}#{utxo['out_ref'][1]}"
    return Address.decode(utxo["address"]), out_ref


def _target(fix: dict[str, Any]) -> dict[str, int]:
    return {unit: int(qty) for unit, qty in fix["collateral"]["collateral_out"].items()}


def _build_modify_tx(
    fix: dict[str, Any],
    snapshot: ModifyCollateralSnapshot,
    fixture: str,
) -> TransactionBuilder:
    """Forward-build the modify from the captured snapshot (no evaluation).

    The eval context's slot is pinned to the captured tx's lower validity bound, so the
    builder reproduces the captured validity window and the validator's interest-time
    matches. Every market forward-synthesizes the oracle ``Withdraw`` from live source
    leaves (``oracle_redeemer=None``); the whole transaction is forward-synthesized.
    """
    assert fixture in FORWARD_FIXTURES
    context = EvalContext(last_block_slot=fix["invalid_before"])
    tx_builder = TransactionBuilder(context)
    build_modify_collateral(
        tx_builder,
        snapshot=snapshot,
        target_collateral=_target(fix),
        txn_time=fix["block_time"] * 1000,
        oracle_redeemer=None,
    )
    actor, actor_utxo = _borrower(fix, snapshot)
    add_modify_collateral_funding(
        tx_builder,
        snapshot=snapshot,
        actor=actor,
        actor_utxo=actor_utxo,
        target_collateral=_target(fix),
    )
    return tx_builder


def _assert_positive_budgets(budgets: list[dict[str, Any]]) -> None:
    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        budget = entry["budget"]
        assert budget["memory"] > 0 and budget["cpu"] > 0


@pytest.mark.parametrize("fixture", FIXTURES)
def test_modify_collateral_evaluates_on_ogmios(
    modify_snap: Callable[[str], tuple[dict, ModifyCollateralSnapshot]],
    fixture: str,
) -> None:
    if not (os.environ.get("OGMIOS_HOST") and (FIXTURES_DIR / fixture).exists()):
        pytest.skip("needs ogmios + the captured modify-collateral fixture")
    fix, snapshot = modify_snap(fixture)
    if fixture in FORWARD_FIXTURES:
        # The forward HF preflight prices the TARGET collateral from the same live
        # leaves the oracle Withdraw reads; a forward-priceable market must clear it.
        assert (
            safe_collateral_for_modify(
                snapshot,
                target_collateral=_target(fix),
                txn_time=fix["block_time"] * 1000,
            )
            > 0
        )
    tx_builder = _build_modify_tx(fix, snapshot, fixture)
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))
    _assert_positive_budgets(budgets)
    purposes = [e["validator"]["purpose"] for e in budgets]
    # loan Spend + oracle Withdraw (no loan hub, no pool hub).
    assert len(budgets) == 2
    assert purposes.count("spend") == 1
    assert purposes.count("withdraw") == 1
    # A modify mints nothing and spends no pool (the loan is the only spend).
    assert tx_builder.mint is None
    assert purposes.count("mint") == 0
    assert len(tx_builder.inputs) == 2  # the loan spend + the borrower funding input
    spent = {
        (u.input.transaction_id.payload.hex(), u.input.index) for u in tx_builder.inputs
    }
    assert snapshot.pool.out_ref not in spent
