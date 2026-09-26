"""Gated: V4 collateral changes evaluate on Ogmios, down to the least collateral."""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    build_change_collateral,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    min_collateral,
)
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios

pytestmark = needs_ogmios

CAPTURES = [("change_collateral_single", 1), ("change_collateral_multi", 3)]


@pytest.mark.parametrize(("name", "loans"), CAPTURES)
def test_captured_change_collateral_evaluates(name: str, loans: int) -> None:
    fix = fixture(name)
    built = build(
        build_change_collateral,
        ChangeCollateralSnapshot.from_capture(fix),
        slot=fix["invalid_before"],
    )
    # Loan spends, loan dispatch + change-collateral action + one oracle withdraw.
    assert evaluate(built, fixture_utxos(fix)) == sorted(
        ["spend"] * loans + ["withdraw"] * 3,
    )


def _at_minimum(name: str, delta: int) -> list[str]:
    fix = fixture(name)
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    for position in snapshot.positions:
        position.new_collateral_amount = (
            min_collateral(
                position.loan_datum,
                valid_to_ms=slot_to_posix_ms(snapshot.valid_to),
                principal_price=snapshot.price(snapshot.principal_oracle(position)),
                collateral_price=snapshot.price(snapshot.collateral_oracle(position)),
            )
            + delta
        )
    built = build(build_change_collateral, snapshot, slot=fix["invalid_before"])
    return evaluate(built, fixture_utxos(fix))


@pytest.mark.parametrize(("name", "loans"), CAPTURES)
def test_the_least_collateral_is_accepted(name: str, loans: int) -> None:
    assert _at_minimum(name, 0) == sorted(["spend"] * loans + ["withdraw"] * 3)


@pytest.mark.parametrize(("name", "loans"), CAPTURES)
def test_one_unit_less_is_rejected(name: str, loans: int) -> None:  # noqa: ARG001
    with pytest.raises(AssertionError, match="ogmios evaluate error"):
        _at_minimum(name, -1)
