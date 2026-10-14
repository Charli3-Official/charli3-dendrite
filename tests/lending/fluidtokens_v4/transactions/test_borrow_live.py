"""Gated: dbsync resolves each captured borrow and Ogmios evaluates it.

The captured pools are spent, so they are resolved with ``allow_spent``; the signed
prices, funding and validity window come from the capture, since a price witness is
time-bound. The scripts come from the live config, so a borrow captured under an
earlier config is evaluated but cannot reproduce its capture: the live config's
reference inputs, and the indexes the redeemers carry, sit elsewhere. Needs
``DBSYNC_*`` and ``OGMIOS_HOST``.
"""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import PoolBorrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import build_borrow
from tests.lending.fluidtokens_v4.transactions.replay import Built
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_purposes
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_dbsync
from tests.lending.fluidtokens_v4.transactions.replay import redeemers

pytestmark = needs_dbsync

CAPTURES = [
    "borrow_single",
    "borrow_multi",
    "borrow_single_oracle_v2",
    "borrow_two_oracle_v2",
]


@pytest.fixture(scope="module")
def backend():  # noqa: ANN201
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _live(
    backend, name: str
) -> tuple[BorrowSnapshot, BorrowSnapshot, Built]:  # noqa: ANN001
    """The capture, its live resolution and that resolution built."""
    fix = fixture(name)
    capture = BorrowSnapshot.from_capture(fix)
    snapshot = BorrowSnapshot.from_backend(
        backend,
        borrows=[
            PoolBorrow(
                leg.out_ref,
                leg.principal_amount,
                leg.chosen_collateral_index,
                leg.collateral_amount,
            )
            for leg in capture.legs
        ],
        borrower_address=capture.borrower_address,
        oracles=capture.oracles,
        funding=capture.funding,
        valid_from=capture.valid_from,
        valid_to=capture.valid_to,
        allow_spent=True,
    )
    return capture, snapshot, build(build_borrow, snapshot, slot=fix["invalid_before"])


@pytest.mark.parametrize("name", CAPTURES)
def test_live_borrow_reproduces_the_capture(backend, name: str) -> None:  # noqa: ANN001
    capture, snapshot, built = _live(backend, name)
    if snapshot.config.out_ref != capture.config.out_ref:
        pytest.skip(
            f"captured under config {capture.config.out_ref}, not the live config "
            f"{snapshot.config.out_ref}, whose reference inputs sit elsewhere",
        )
    assert redeemers(built.tx) == captured_redeemers(fixture(name))


@pytest.mark.parametrize("name", CAPTURES)
def test_live_borrow_evaluates(backend, name: str) -> None:  # noqa: ANN001
    fix = fixture(name)
    _, snapshot, built = _live(backend, name)
    scripts = [
        snapshot.config,
        snapshot.pool_spend_script_ref,
        snapshot.pool_policy_script_ref,
        snapshot.borrow_action_script_ref,
        snapshot.loan_policy_script_ref,
        snapshot.lender_bond_policy_script_ref,
        snapshot.borrower_bond_policy_script_ref,
    ]
    # Pool spends, three mints, pool dispatch + borrow action + the oracle withdraws.
    assert evaluate(built, fixture_utxos(fix) + scripts) == captured_purposes(fix)
