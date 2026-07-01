"""Gated end-to-end: a forward-built increase-loan evaluates on Ogmios.

The decisive correctness proof for the increase-loan (IncreaseLoanAmount) builder. It
is self-contained: rather than resolving live pool/loan state (the captured loan is
already spent), it reconstructs an `IncreaseLoanSnapshot` from a captured real on-chain
increase-loan, forward-builds a brand-new (unsigned, unsubmitted) increase against the
captured spent inputs, and asks the real Ogmios to execute every Plutus script via
``evaluateTransaction`` -- feeding the spent inputs + reference UTxOs back through
``additionalUtxo`` (spent/historical UTxOs supplied that way are stable for evaluation,
independent of any later on-chain reference-script rotation).

An increase returns FOUR positive execution budgets: the pool ``Spend``, the loan
``Spend``, the novel ``Withdraw(pool_skh)`` orchestration hub, and the oracle
``Withdraw`` price calc. It mints NOTHING (the loan token + owner NFT already exist).
The validity window is pinned to the captured transaction's so the validator's interest
accrual matches the synthesized datums. Nothing is signed or submitted.
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
    add_increase_funding,
)
from charli3_dendrite.lending.danogo.transactions.build import (  # noqa: E402
    build_increase_loan,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    IncreaseLoanSnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE = "increase_loan_tx.json"

# Ogmios `additionalUtxo` reference-script language tags, keyed by db-sync's Plutus
# version label (all three Danogo reference scripts are PlutusV3).
_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _gate(name: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (os.environ.get("OGMIOS_HOST") and (FIXTURES_DIR / name).exists()),
        reason="needs ogmios + the captured increase-loan fixture",
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

    Every UTxO the forward-built increase spends (pool, loan, borrower) or reads (the
    protocol-config / market / oracle reference inputs + the three reference scripts) is
    one of the captured tx's now-spent inputs or reference inputs, so the whole set is
    supplied; the borrower input -- the one carrying the loan's owner NFT -- is among
    the captured inputs.
    """
    return [_utxo_entry(u) for u in fix["inputs"] + fix["ref_inputs"]]


def _borrower(
    fix: dict[str, Any], snapshot: IncreaseLoanSnapshot
) -> tuple[Address, str]:
    """The captured borrower (funding) input's address + out-ref: it holds owner NFT.

    The owner NFT (qty 1) proves loan ownership; the increase spends the borrower input
    holding it (nothing is burned). It is one of the captured inputs, so the same
    out-ref is resolved from the `additionalUtxo` set.
    """
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


def _build_increase_tx(
    fix: dict[str, Any],
    snapshot: IncreaseLoanSnapshot,
) -> TransactionBuilder:
    """Forward-build the increase from the captured snapshot (no evaluation).

    The eval context's slot is pinned to the captured tx's lower validity bound, so the
    builder reproduces the captured validity window and the validator's interest-time
    matches the synthesized datums. The borrow amount is the captured supply paid out
    (``-pool_changed_amount``).
    """
    realized = fix["realized"]
    context = EvalContext(last_block_slot=fix["invalid_before"])
    tx_builder = TransactionBuilder(context)
    build_increase_loan(
        tx_builder,
        snapshot=snapshot,
        borrow_amount=-realized["pool_changed_amount"],
        txn_time=realized["txn_time"],
    )
    actor, actor_utxo = _borrower(fix, snapshot)
    add_increase_funding(
        tx_builder,
        snapshot=snapshot,
        actor=actor,
        actor_utxo=actor_utxo,
    )
    return tx_builder


def _assert_positive_budgets(budgets: list[dict[str, Any]]) -> None:
    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        budget = entry["budget"]
        assert budget["memory"] > 0 and budget["cpu"] > 0


@_gate(FIXTURE)
def test_increase_loan_evaluates_on_ogmios(
    increase_snap: Callable[[str], tuple[dict, IncreaseLoanSnapshot]],
) -> None:
    fix, snapshot = increase_snap(FIXTURE)
    tx_builder = _build_increase_tx(fix, snapshot)
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))
    _assert_positive_budgets(budgets)
    purposes = [e["validator"]["purpose"] for e in budgets]
    # pool Spend + loan Spend + Withdraw(pool_skh) hub + oracle Withdraw (no mint).
    assert len(budgets) == 4
    # The pool-script hub and the oracle price calc both run under the withdraw purpose.
    assert purposes.count("withdraw") == 2
    assert purposes.count("spend") == 2
    # An increase mints nothing.
    assert tx_builder.mint is None
    assert purposes.count("mint") == 0
