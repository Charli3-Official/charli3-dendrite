"""Gated end-to-end: a 100 ADA deposit on the large ADA-supply pool evaluates.

The decisive correctness proof that the previously-blocked pool now builds AND
validates. The pool's market lists a disallowed, never-held, un-priceable alternative
supply token; before the `_alt_supply_update` fix the builder raised trying to price it,
so no deposit/withdraw could be assembled at all.

This is self-contained (mirroring the increase-loan e2e): it reconstructs a
`TopupWithdrawSnapshot` from a captured real deposit/withdraw on this pool,
forward-builds a brand-new (unsigned, unsubmitted) 100 ADA deposit against the captured
spent inputs, and asks the real Ogmios to execute every Plutus script via
``evaluateTransaction`` -- feeding the spent inputs + reference UTxOs back through
``additionalUtxo``. A deposit returns THREE positive execution budgets: the pool
``Spend``, the dToken ``Mint``, and the oracle ``Withdraw`` price calc (the alt-supply
revaluation re-prices the held token and carries the un-priceable one). The validity
window is pinned to the captured transaction's so the validator's interest accrual
matches the synthesized datum. Nothing is signed or submitted.
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

from charli3_dendrite.lending.danogo.datums import PoolDatum  # noqa: E402
from charli3_dendrite.lending.danogo.transactions._common import (  # noqa: E402
    add_actor_funding,
)
from charli3_dendrite.lending.danogo.transactions.build import (  # noqa: E402
    build_topup_withdraw,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    TopupWithdrawSnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE = "topup_zero_held_alt_tx.json"

# A forward-built 100 ADA deposit (the supply token is ADA for this pool).
_DEPOSIT_LOVELACE = 100_000_000

_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _gate(name: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (os.environ.get("OGMIOS_HOST") and (FIXTURES_DIR / name).exists()),
        reason="needs ogmios + the captured deposit/withdraw fixture",
    )


def _utxo_entry(u: dict[str, Any]) -> dict[str, Any]:
    """An Ogmios `additionalUtxo` entry resolving one captured (spent) UTxO."""
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
    """The captured spent inputs + reference inputs as the Ogmios `additionalUtxo` set.

    Every UTxO the forward-built deposit spends (the pool + the actor funding input) or
    reads (the protocol-config / market / oracle reference inputs + the reference
    scripts) is one of the captured tx's now-spent inputs or reference inputs, so the
    whole set is supplied; the actor input -- a plain wallet UTxO holding ample ADA --
    is among the captured inputs.
    """
    return [_utxo_entry(u) for u in fix["inputs"] + fix["ref_inputs"]]


def _actor(fix: dict[str, Any]) -> tuple[Address, str]:
    """The captured non-pool wallet input: it funds the deposit's supply (ADA)."""
    actor_in = next(u for u in fix["inputs"] if not _parses_pool_datum(u.get("datum")))
    out_ref = f"{actor_in['out_ref'][0]}#{actor_in['out_ref'][1]}"
    return Address.decode(actor_in["address"]), out_ref


def _parses_pool_datum(datum: str | None) -> bool:
    if not datum:
        return False
    try:
        PoolDatum.from_cbor(datum)
        return True
    except Exception:  # noqa: BLE001 - any decode failure means "not the pool"
        return False


def _build_deposit_tx(
    fix: dict[str, Any],
    snapshot: TopupWithdrawSnapshot,
) -> TransactionBuilder:
    """Forward-build the 100 ADA deposit from the captured snapshot (no evaluation).

    The eval context's slot is pinned to the captured tx's lower validity bound and the
    datum-synthesis time to the captured tx's, so the builder reproduces the captured
    validity window and the validator's interest-time matches the synthesized datum.
    """
    realized = fix["realized"]
    context = EvalContext(last_block_slot=fix["invalid_before"])
    tx_builder = TransactionBuilder(context)
    actor, actor_utxo = _actor(fix)
    build_topup_withdraw(
        tx_builder,
        snapshot=snapshot,
        actor_address=actor,
        supply_change=_DEPOSIT_LOVELACE,
        txn_time=realized["txn_time"],
    )
    add_actor_funding(
        tx_builder,
        actor=actor,
        actor_utxo=actor_utxo,
        funding={"lovelace": _DEPOSIT_LOVELACE},
    )
    return tx_builder


def _assert_positive_budgets(budgets: list[dict[str, Any]]) -> None:
    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        budget = entry["budget"]
        assert budget["memory"] > 0 and budget["cpu"] > 0


@_gate(FIXTURE)
def test_zero_held_alt_deposit_evaluates_on_ogmios(
    topup_snap: Callable[[str], tuple[dict, TopupWithdrawSnapshot]],
) -> None:
    fix, snapshot = topup_snap(FIXTURE)
    tx_builder = _build_deposit_tx(fix, snapshot)
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))
    _assert_positive_budgets(budgets)
    purposes = [e["validator"]["purpose"] for e in budgets]
    # pool Spend + dToken Mint + oracle Withdraw (the alt-supply pool re-prices on it).
    assert len(budgets) == 3
    assert purposes.count("spend") == 1
    assert purposes.count("mint") == 1
    assert purposes.count("withdraw") == 1
    # A deposit mints the dToken (positive amount).
    assert tx_builder.mint is not None
