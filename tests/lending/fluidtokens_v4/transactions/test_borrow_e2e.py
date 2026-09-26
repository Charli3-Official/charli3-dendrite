"""Gated: forward-built V4 borrows evaluate on Ogmios against mainnet (OGMIOS_HOST)."""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_collateral_amount,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import build_borrow
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios

pytestmark = needs_ogmios


@pytest.mark.parametrize(
    ("name", "pools"),
    [("borrow_single", 1), ("borrow_multi", 6)],
)
def test_borrow_evaluates(name: str, pools: int) -> None:
    fix = fixture(name)
    built = build(
        build_borrow, BorrowSnapshot.from_capture(fix), slot=fix["invalid_before"]
    )
    # Pool spends, three mints, pool dispatch + borrow action + one oracle withdraw.
    assert evaluate(built, fixture_utxos(fix)) == sorted(
        ["spend"] * pools + ["mint"] * 3 + ["withdraw"] * 3,
    )


def _at_minimum(delta: int) -> list[str]:
    """Evaluate borrow_single with the least collateral the pool accepts, plus delta."""
    fix = fixture("borrow_single")
    snapshot = BorrowSnapshot.from_capture(fix)
    (leg,) = snapshot.legs
    reward = snapshot.oracle_for(leg).reward
    leg.collateral_amount = (
        min_collateral_amount(
            leg.pool_datum,
            chosen_collateral_index=leg.chosen_collateral_index,
            principal_amount=leg.principal_amount,
            price_num=reward.price_num,
            price_den=reward.price_den,
        )
        + delta
    )
    built = build(build_borrow, snapshot, slot=fix["invalid_before"])
    return evaluate(built, fixture_utxos(fix))


def test_the_least_collateral_is_accepted() -> None:
    assert _at_minimum(0) == sorted(["spend"] + ["mint"] * 3 + ["withdraw"] * 3)


def test_one_unit_less_is_rejected() -> None:
    with pytest.raises(AssertionError, match="ogmios evaluate error"):
        _at_minimum(-1)
