"""Gated: V4 recasts evaluate on Ogmios against mainnet (OGMIOS_HOST).

No recast exists on mainnet, and the recast action's reward account is not yet
registered, so a recast cannot be submitted today; Ogmios evaluates the scripts
without checking registration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from charli3_dendrite.lending.fluidtokens.datums import InterestOnRemainingPrincipal
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    remaining_debt,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import build_recast
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolTrue
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.fluidtokens_v4.transactions.recast_setup import recast_batch_snapshot
from tests.lending.fluidtokens_v4.transactions.recast_setup import recast_snapshot
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios

pytestmark = needs_ogmios

_CONTINUES = ["spend", "withdraw", "withdraw"]


def _amortized(datum: LoanDatum) -> LoanDatum:
    return replace(
        datum,
        repayment_mode=InterestOnRemainingPrincipal(max_possible_recasts=3),
        total_installments=6,
        installment_period=720,
        interest_rate=1200,
        repaid_installments=1,
    )


@pytest.mark.parametrize(
    ("edit", "amount_paid", "receipts", "purposes"),
    [
        (lambda d: d, 10_000_000, False, _CONTINUES),
        (lambda d: d, 5, False, _CONTINUES),
        (
            lambda d: replace(d, repayment_receipts=BoolTrue()),
            10_000_000,
            True,
            ["mint", *_CONTINUES],
        ),
        (_amortized, 5_000_000, False, _CONTINUES),
    ],
    ids=["perpetual-half", "perpetual-interest-plus-one", "with-receipt", "amortized"],
)
def test_recast_evaluates(
    edit: Callable[[LoanDatum], LoanDatum],
    amount_paid: int,
    receipts: bool,  # noqa: FBT001
    purposes: list[str],
) -> None:
    snapshot, utxos, slot = recast_snapshot(
        edit,
        amount_paid=amount_paid,
        receipts=receipts,
    )
    assert evaluate(build(build_recast, snapshot, slot=slot), utxos) == sorted(purposes)


def test_a_closing_recast_evaluates() -> None:
    snapshot, utxos, slot = recast_snapshot(amount_paid=0)
    (position,) = snapshot.positions
    position.amount_paid = remaining_debt(
        position.loan_datum,
        valid_to_ms=slot_to_posix_ms(snapshot.valid_to),
    )
    assert evaluate(build(build_recast, snapshot, slot=slot), utxos) == sorted(
        ["mint", *_CONTINUES],
    )


def test_a_mixed_batch_evaluates() -> None:
    snapshot, utxos, slot = recast_batch_snapshot([10_000_000, 10_000_000, 10_000_000])
    # First loan continues, second and third close
    valid_to_ms = slot_to_posix_ms(snapshot.valid_to)
    snapshot.positions[0].amount_paid = 5_000_000
    snapshot.positions[1].amount_paid = remaining_debt(
        snapshot.positions[1].loan_datum, valid_to_ms=valid_to_ms
    )
    snapshot.positions[2].amount_paid = remaining_debt(
        snapshot.positions[2].loan_datum, valid_to_ms=valid_to_ms
    )
    assert evaluate(build(build_recast, snapshot, slot=slot), utxos) == sorted(
        ["spend"] * 3 + ["mint", "withdraw", "withdraw"],
    )
