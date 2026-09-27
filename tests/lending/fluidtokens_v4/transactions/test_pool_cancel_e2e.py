"""Gated: V4 pool cancels evaluate on Ogmios against mainnet (OGMIOS_HOST).

No mainnet transaction cancels several pools at once, so those spend a copy of the
captured pool and its manager at other out-refs: Ogmios reads both from
``additionalUtxo``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    PoolCancelSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    build_pool_cancel,
)
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios
from tests.lending.fluidtokens_v4.transactions.replay import second_pool

pytestmark = needs_ogmios

# Pool and manager spends, both burns, and the four withdrawals.
_CANCEL = sorted(["spend", "spend", "mint", "mint"] + ["withdraw"] * 4)


def _capture() -> tuple[dict, PoolCancelSnapshot]:
    fix = fixture("pool_cancel")
    return fix, PoolCancelSnapshot.from_capture(fix)


def test_captured_cancel_evaluates() -> None:
    fix, snapshot = _capture()
    built = build(build_pool_cancel, snapshot, slot=fix["invalid_before"])
    assert evaluate(built, fixture_utxos(fix)) == _CANCEL


def test_a_cancel_burning_against_its_pool_evaluates() -> None:
    fix, snapshot = _capture()
    (position,) = snapshot.positions
    built = build(
        build_pool_cancel,
        replace(snapshot, funding=[], mint_input_ref=position.out_ref),
        slot=fix["invalid_before"],
    )
    assert evaluate(built, fixture_utxos(fix)) == _CANCEL


def test_two_pools_cancelled_together_evaluate() -> None:
    fix, snapshot = _capture()
    (position,) = snapshot.positions
    # The captured pool sits at 69dd0c80#0, its manager at 51160fea#1.
    other = second_pool(position, pool_ref=("70" * 32, 0), manager_ref=("52" * 32, 0))
    built = build(
        build_pool_cancel,
        replace(snapshot, positions=[position, other]),
        slot=fix["invalid_before"],
    )
    utxos = fixture_utxos(fix) + [other.pool, other.pool_manager]
    assert evaluate(built, utxos) == sorted(_CANCEL + ["spend", "spend"])


def test_pools_whose_managers_sort_differently_fail_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Why the builder refuses them: the owner check pairs pools and managers by
    # input position, so no order of names satisfies both.
    from charli3_dendrite.lending.fluidtokens_v4.transactions import pool_action

    monkeypatch.setattr(pool_action, "require_paired_order", lambda _positions: None)
    fix, snapshot = _capture()
    (position,) = snapshot.positions
    other = second_pool(position, pool_ref=("00" * 32, 0), manager_ref=("52" * 32, 0))
    built = build(
        build_pool_cancel,
        replace(snapshot, positions=[position, other]),
        slot=fix["invalid_before"],
    )
    with pytest.raises(AssertionError, match="ogmios evaluate error"):
        evaluate(built, fixture_utxos(fix) + [other.pool, other.pool_manager])
