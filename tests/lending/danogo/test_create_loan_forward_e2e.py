"""Gated end-to-end: a freshly-built forward create-loan evaluates on Ogmios.

This is the ultimate correctness proof for the forward builder: it resolves live
market state, derives a safe borrow amount from the live oracle price + market
threshold, assembles a brand-new (unsigned, unsubmitted) create-loan transaction,
and asks the real Ogmios to execute every Plutus script via ``evaluateTransaction``.
A successful run returns one positive execution budget per redeemer (pool spend,
loan mint, oracle withdraw), proving the synthesized redeemers/datums/indices are
accepted by the on-chain validators. Nothing is signed or submitted.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

load_dotenv()
from charli3_dendrite.backend.dbsync import DbsyncBackend  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.build import (  # noqa: E402
    build_and_evaluate,
)
from charli3_dendrite.lending.danogo.transactions.builder import (  # noqa: E402
    DanogoTxBuilder,
)
from charli3_dendrite.lending.transactions.base import ActionParams  # noqa: E402
from charli3_dendrite.lending.transactions.base import LendingAction  # noqa: E402

MARKET = os.environ.get("DANOGO_TEST_MARKET", "")
ACTOR = os.environ.get("DANOGO_TEST_ACTOR_ADDR", "")
ACTOR_UTXO = os.environ.get("DANOGO_TEST_ACTOR_UTXO", "")

_GATED = pytest.mark.skipif(
    not (
        os.environ.get("DBSYNC_HOST")
        and os.environ.get("OGMIOS_HOST")
        and MARKET
        and ACTOR_UTXO
    ),
    reason="needs db-sync + ogmios + DANOGO_TEST_* knobs",
)


def _collateral() -> dict[str, int]:
    return {
        os.environ["DANOGO_TEST_COLLATERAL_UNIT"]: int(
            os.environ["DANOGO_TEST_COLLATERAL_QTY"],
        ),
    }


def _assert_positive_budgets(budgets: list[dict]) -> None:
    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        b = entry["budget"]
        assert b["memory"] > 0 and b["cpu"] > 0


@_GATED
def test_forward_create_loan_evaluates_on_ogmios():
    budgets = build_and_evaluate(
        DbsyncBackend(),
        market_name=MARKET,
        actor_address=ACTOR,
        actor_utxo=ACTOR_UTXO,
        collateral=_collateral(),
        borrow_amount=None,  # derive a safe amount from live collateral value
    )
    _assert_positive_budgets(budgets)


@_GATED
def test_seam_borrow_evaluates_on_ogmios():
    # The seam-driven BORROW path must produce the SAME positive budgets as the direct
    # `build_and_evaluate` above: resolve snapshot -> contribute create-loan + borrower
    # funding -> assemble unsigned -> evaluate via `snapshot.additional_utxo()`.
    budgets = DanogoTxBuilder().build_and_evaluate(
        backend=DbsyncBackend(),
        market_name=MARKET,
        action=LendingAction.BORROW,
        params=ActionParams(
            actor_address=ACTOR,
            actor_utxo=ACTOR_UTXO,
            collateral=_collateral(),
            borrow_amount=None,  # derive a safe amount from live collateral value
        ),
    )
    _assert_positive_budgets(budgets)
    # pool Spend + loan Mint + oracle Withdraw
    assert len(budgets) == 3
