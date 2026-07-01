"""Gated end-to-end: a freshly-built deposit/withdraw evaluates on Ogmios.

The correctness proof for the deposit/withdraw (TopupWithdraw) builder: it resolves
live pool state, assembles a brand-new (unsigned, unsubmitted) deposit or withdraw
transaction, and asks the real Ogmios to execute every Plutus script via
``evaluateTransaction``. A successful run returns one positive execution budget per
redeemer (the pool spend + the dToken mint/burn, both driven by the same
``TopupWithdraw`` redeemer), proving the synthesized datum / mint amount / indices
are accepted by the on-chain validator. Nothing is signed or submitted.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

load_dotenv()
from charli3_dendrite.backend.dbsync import DbsyncBackend  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.builder import (  # noqa: E402
    DanogoTxBuilder,
)
from charli3_dendrite.lending.transactions.base import ActionParams  # noqa: E402
from charli3_dendrite.lending.transactions.base import LendingAction  # noqa: E402

TW_MARKET = os.environ.get("DANOGO_TEST_TW_MARKET", "")
DEP_ADDR = os.environ.get("DANOGO_TEST_TOPUP_ACTOR_ADDR", "")
DEP_UTXO = os.environ.get("DANOGO_TEST_TOPUP_ACTOR_UTXO", "")
WD_ADDR = os.environ.get("DANOGO_TEST_WITHDRAW_ACTOR_ADDR", "")
WD_UTXO = os.environ.get("DANOGO_TEST_WITHDRAW_ACTOR_UTXO", "")

ALT_MARKET = os.environ.get("DANOGO_TEST_ALT_MARKET", "")
ALT_ADDR = os.environ.get("DANOGO_TEST_ALT_TOPUP_ADDR", "")
ALT_UTXO = os.environ.get("DANOGO_TEST_ALT_TOPUP_UTXO", "")


def _assert_positive_budgets(budgets: list[dict]) -> None:
    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        b = entry["budget"]
        assert b["memory"] > 0 and b["cpu"] > 0


def _gate(utxo: str):
    return pytest.mark.skipif(
        not (
            os.environ.get("DBSYNC_HOST")
            and os.environ.get("OGMIOS_HOST")
            and TW_MARKET
            and utxo
        ),
        reason="needs db-sync + ogmios + DANOGO_TEST_TW_* actor",
    )


@_gate(DEP_UTXO)
def test_deposit_evaluates_on_ogmios():
    budgets = DanogoTxBuilder().build_and_evaluate(
        backend=DbsyncBackend(),
        market_name=TW_MARKET,
        action=LendingAction.DEPOSIT,
        params=ActionParams(
            actor_address=DEP_ADDR,
            actor_utxo=DEP_UTXO,
            # Must exceed the market's min_tx_amount; small amounts the validator
            # rejects are covered by the offline guard test, not this live path.
            amount=int(os.environ.get("DANOGO_TEST_DEPOSIT", "25000000")),
        ),
    )
    _assert_positive_budgets(budgets)
    # pool Spend + pool Mint (no oracle for this no-alt market)
    assert len(budgets) == 2


@pytest.mark.skipif(
    not (
        os.environ.get("DBSYNC_HOST")
        and os.environ.get("OGMIOS_HOST")
        and ALT_MARKET
        and ALT_UTXO
    ),
    reason="needs an alt-supply-token market + actor holding its supply token",
)
def test_alt_deposit_oracle_withdraw_evaluates():
    # An alt-supply-token market re-prices its alternative supply tokens from the
    # oracle on every deposit, so the build attaches the oracle Withdraw redeemer in
    # addition to the pool Spend + pool Mint -- three positive budgets total, with the
    # oracle price-calc running under the `withdraw` purpose.
    budgets = DanogoTxBuilder().build_and_evaluate(
        backend=DbsyncBackend(),
        market_name=ALT_MARKET,
        action=LendingAction.DEPOSIT,
        params=ActionParams(
            actor_address=ALT_ADDR,
            actor_utxo=ALT_UTXO,
            # Must exceed the alt market's min_tx_amount (4999995).
            amount=int(os.environ.get("DANOGO_TEST_ALT_DEPOSIT", "5000000")),
        ),
    )
    _assert_positive_budgets(budgets)
    purposes = {e["validator"]["purpose"] for e in budgets}
    assert "withdraw" in purposes  # oracle price-calc ran
    assert len(budgets) == 3


@_gate(WD_UTXO)
def test_withdraw_evaluates_on_ogmios():
    budgets = DanogoTxBuilder().build_and_evaluate(
        backend=DbsyncBackend(),
        market_name=TW_MARKET,
        action=LendingAction.WITHDRAW,
        params=ActionParams(
            actor_address=WD_ADDR,
            actor_utxo=WD_UTXO,
            # Must exceed the market's min_tx_amount (see deposit note above).
            amount=int(os.environ.get("DANOGO_TEST_WITHDRAW", "25000000")),
        ),
    )
    _assert_positive_budgets(budgets)
    # pool Spend + pool Mint (no oracle for this no-alt market)
    assert len(budgets) == 2
