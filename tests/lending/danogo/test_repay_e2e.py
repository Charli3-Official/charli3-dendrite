"""Gated end-to-end: a forward-built repay (full / partial / collateral-mod) on Ogmios.

The decisive correctness proof for the repay (DecreaseLoanAmount) builder. It is
self-contained: rather than resolving live pool/loan state (the captured loans are
already closed/spent), it reconstructs a `RepaySnapshot` from a captured real
on-chain repay, forward-builds a brand-new (unsigned, unsubmitted) repay against the
captured spent inputs, and asks the real Ogmios to execute every Plutus script via
``evaluateTransaction`` -- feeding the spent inputs + reference UTxOs back through
``additionalUtxo`` (spent/historical UTxOs supplied that way are stable for
evaluation, independent of any later on-chain reference-script rotation).

A full repay returns five positive execution budgets (pool ``Spend``, loan
``Spend``, the ``Withdraw(loan_skh)`` orchestration hub, the oracle ``Withdraw``
price calc, and the loan ``Mint`` burn); a partial repay returns four (no mint). A
partial repay that ALSO modifies collateral folds the change into that same partial
repay -- same four budgets -- and the built loan output locks the target collateral.
The validity window is pinned to the captured transaction's so the validator's
interest accrual matches the synthesized datums. Nothing is signed or submitted.
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
    add_repay_funding,
)
from charli3_dendrite.lending.danogo.transactions.build import build_repay  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    RepaySnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FULL = "decrease_loan_tx.json"
PARTIAL = "decrease_loan_partial_tx.json"
REMOVE_COLLAT = "decrease_loan_remove_collat_tx.json"
ADD_COLLAT = "decrease_loan_add_collat_tx.json"

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
        reason="needs ogmios + the captured decrease-loan fixture",
    )


def _utxo_entry(u: dict[str, Any]) -> dict[str, Any]:
    """An Ogmios `additionalUtxo` entry resolving one captured (spent) UTxO.

    Carries the UTxO's real captured value, inline datum, and (for the reference
    scripts) the attached Plutus script, so Ogmios resolves the spent inputs and
    reference inputs from the supplied set rather than from its live ledger.
    """
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

    Every UTxO the forward-built repay spends (pool, loan, borrower) or reads (the
    protocol-config / market / oracle reference inputs + the three reference scripts)
    is one of the captured tx's now-spent inputs or reference inputs, so the whole set
    is supplied; the borrower input -- the one carrying the loan's owner NFT -- is
    among the captured inputs.
    """
    return [_utxo_entry(u) for u in fix["inputs"] + fix["ref_inputs"]]


def _borrower(fix: dict[str, Any], snapshot: RepaySnapshot) -> tuple[Address, str]:
    """The captured borrower (funding) input's address + out-ref: it holds owner NFT.

    The owner NFT (qty 1) proves loan ownership; the repay spends the borrower input
    holding it (and, on a full repay, burns it). It is one of the captured inputs, so
    the same out-ref is resolved from the `additionalUtxo` set.
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


def _build_repay_tx(
    fix: dict[str, Any],
    snapshot: RepaySnapshot,
    *,
    collateral: dict[str, int] | None = None,
) -> TransactionBuilder:
    """Forward-build the repay from the captured snapshot (no evaluation).

    The eval context's slot is pinned to the captured tx's lower validity bound, so the
    builder reproduces the captured validity window and the validator's interest-time
    matches the synthesized datums (the same instant the captured `txn_time` encodes).
    A `collateral` target threads through both the loan output (`build_repay`) and the
    borrower funding (`add_repay_funding`) for a collateral-modifying partial repay.
    """
    realized = fix["realized"]
    context = EvalContext(last_block_slot=fix["invalid_before"])
    tx_builder = TransactionBuilder(context)
    build_repay(
        tx_builder,
        snapshot=snapshot,
        amount=realized["pool_change_amount"],
        collateral=collateral,
        txn_time=realized["txn_time"],
    )
    actor, actor_utxo = _borrower(fix, snapshot)
    add_repay_funding(
        tx_builder,
        snapshot=snapshot,
        actor=actor,
        actor_utxo=actor_utxo,
        amount=realized["pool_change_amount"],
        collateral=collateral,
    )
    return tx_builder


def _build_and_evaluate(
    fix: dict[str, Any],
    snapshot: RepaySnapshot,
    *,
    collateral: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Forward-build the repay from the captured snapshot and evaluate it on Ogmios."""
    tx_builder = _build_repay_tx(fix, snapshot, collateral=collateral)
    cbor = assemble_unsigned(tx_builder)
    return evaluate_tx_cbor(cbor, _additional_utxo(fix))


def _built_loan_collateral(
    tx_builder: TransactionBuilder,
    snapshot: RepaySnapshot,
) -> dict[str, int]:
    """The collateral the built loan output locks (its native assets minus loan token).

    The loan output is the one carrying the loan token (qty 1); everything else in its
    multi-asset value is the locked collateral.
    """
    loan_token = snapshot.loan_skh + snapshot.market_name
    for output in tx_builder.outputs:
        flat = {
            bytes(policy).hex() + bytes(name).hex(): qty
            for policy, names in output.amount.multi_asset.data.items()
            for name, qty in names.items()
        }
        if flat.get(loan_token) == 1:
            return {unit: qty for unit, qty in flat.items() if unit != loan_token}
    raise AssertionError("built loan output not found")


def _assert_positive_budgets(budgets: list[dict[str, Any]]) -> None:
    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        budget = entry["budget"]
        assert budget["memory"] > 0 and budget["cpu"] > 0


@_gate(FULL)
def test_full_repay_evaluates_on_ogmios(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    fix, snapshot = repay_snap(FULL)
    budgets = _build_and_evaluate(fix, snapshot)
    _assert_positive_budgets(budgets)
    purposes = [e["validator"]["purpose"] for e in budgets]
    # pool Spend + loan Spend + loan Mint burn + Withdraw(loan_skh) hub + oracle Withdraw
    assert len(budgets) == 5
    # The loan-script hub and the oracle price calc both run under the withdraw purpose.
    assert purposes.count("withdraw") == 2


# The captured partial repay locks the pool's dToken as collateral while the market's
# supply token differs from that dToken's native pool quote, so the collateral is
# priced through an intermediate quote (ada): the oracle Withdraw declares the
# intermediate ada->supply price AND composes the dToken->supply price from it (the
# dToken->ada pool leg times ada->supply), referencing both legs' source leaves.
@_gate(PARTIAL)
def test_partial_repay_evaluates_on_ogmios(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    fix, snapshot = repay_snap(PARTIAL)
    budgets = _build_and_evaluate(fix, snapshot)
    _assert_positive_budgets(budgets)
    purposes = [e["validator"]["purpose"] for e in budgets]
    # pool Spend + loan Spend + Withdraw(loan_skh) hub + oracle Withdraw (no mint burn)
    assert len(budgets) == 4
    assert purposes.count("withdraw") == 2


# A partial repay that ALSO removes collateral in the same transaction: the captured
# tx withdraws -9,000,000,000 of the loan's dToken collateral, leaving a 6,000,000,000
# target locked in the reduced loan output. The removed collateral returns to the
# borrower via balancing; the modification rides the same DecreaseLoanAmount redeemer.
#
# The fixture's market quotes in a non-ADA supply token (cross-quote): the collateral
# is priced through an ada->quote intermediate the routing path config averages across
# several price sources and deviation-checks. The forward-built oracle Withdraw routes
# the source leaves by that path config, referencing the recipe legs plus the path
# config's cross-check source (an Indigo ada->quote feed) the validator walks, so the
# referenced leaf set matches the on-chain redeemer and the oracle Withdraw evaluates
# positive alongside the pool/loan spends and the hub.
@_gate(REMOVE_COLLAT)
def test_collateral_remove_repay_evaluates(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    fix, snapshot = repay_snap(REMOVE_COLLAT)
    target = {
        unit: int(qty)
        for unit, qty in fix["realized"]["collateral"]["collateral_out"].items()
    }
    tx_builder = _build_repay_tx(fix, snapshot, collateral=target)
    # The built loan output locks exactly the target collateral (post-removal).
    assert _built_loan_collateral(tx_builder, snapshot) == target
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))
    _assert_positive_budgets(budgets)
    purposes = [e["validator"]["purpose"] for e in budgets]
    # pool Spend + loan Spend + Withdraw(loan_skh) hub + oracle Withdraw (no mint burn)
    assert len(budgets) == 4
    assert purposes.count("withdraw") == 2


# A partial repay that ADDS collateral in the same transaction, against the newer
# (structured) oracle deployment: the borrower funds the positive collateral delta, so
# the built loan output locks the larger target collateral. This market's oracle prices
# the dToken collateral by walking the on-chain structured path config (source 24
# Danogo pool forward * source 18 Indigo reversed) with the ADA intermediate declared
# first, rather than a mined recipe -- so the oracle Withdraw evaluates positive
# alongside the pool/loan spends and the hub, proving the structured path-walk pricing
# matches the on-chain validator.
@_gate(ADD_COLLAT)
def test_collateral_add_repay_evaluates(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    fix, snapshot = repay_snap(ADD_COLLAT)
    target = {
        unit: int(qty)
        for unit, qty in fix["realized"]["collateral"]["collateral_out"].items()
    }
    tx_builder = _build_repay_tx(fix, snapshot, collateral=target)
    # The built loan output locks exactly the target collateral (post-addition).
    assert _built_loan_collateral(tx_builder, snapshot) == target
    cbor = assemble_unsigned(tx_builder)
    budgets = evaluate_tx_cbor(cbor, _additional_utxo(fix))
    _assert_positive_budgets(budgets)
    purposes = [e["validator"]["purpose"] for e in budgets]
    # pool Spend + loan Spend + Withdraw(loan_skh) hub + oracle Withdraw (no mint burn)
    assert len(budgets) == 4
    assert purposes.count("withdraw") == 2
