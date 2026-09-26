"""Gated: dbsync resolves each captured collateral change and Ogmios evaluates it."""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    build_change_collateral,
)
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_dbsync

pytestmark = needs_dbsync


@pytest.fixture(scope="module")
def backend():  # noqa: ANN201
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _resolve(
    backend, capture: ChangeCollateralSnapshot, **overrides: object
):  # noqa: ANN001, ANN202
    kwargs: dict = {
        "changes": [(p.out_ref, p.new_collateral_amount) for p in capture.positions],
        "borrower_address": capture.positions[0].borrower_bond.address,
        "oracles": capture.oracles,
        "funding": capture.funding,
        "valid_from": capture.valid_from,
        "valid_to": capture.valid_to,
        "allow_spent": True,
    }
    return ChangeCollateralSnapshot.from_backend(backend, **(kwargs | overrides))


@pytest.mark.parametrize(
    "name", ["change_collateral_single", "change_collateral_multi"]
)
def test_live_change_collateral_evaluates(backend, name: str) -> None:  # noqa: ANN001
    fix = fixture(name)
    snapshot = _resolve(backend, ChangeCollateralSnapshot.from_capture(fix))
    built = build(build_change_collateral, snapshot, slot=fix["invalid_before"])
    utxos = fixture_utxos(fix) + [
        u for p in snapshot.positions for u in (p.loan, p.borrower_bond)
    ]
    scripts = [
        snapshot.config,
        snapshot.loan_spend_script_ref,
        snapshot.loan_policy_script_ref,
        snapshot.action_script_ref,
    ]
    # Loan spends, loan dispatch + change-collateral action + one oracle withdraw.
    assert evaluate(built, utxos + scripts) == sorted(
        ["spend"] * len(snapshot.positions) + ["withdraw"] * 3,
    )


def test_live_change_collateral_refuses_too_little(backend) -> None:  # noqa: ANN001
    capture = ChangeCollateralSnapshot.from_capture(fixture("change_collateral_single"))
    (position,) = capture.positions
    with pytest.raises(ValueError, match="must keep at least 1219978267"):
        _resolve(backend, capture, changes=[(position.out_ref, 1_219_978_266)])
