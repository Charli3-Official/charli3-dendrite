"""Gated: V4 pool edits evaluate on Ogmios against mainnet (OGMIOS_HOST).

No mainnet transaction edits several pools at once, so that edit spends a copy of
the captured pool and its manager at other out-refs: Ogmios reads both from
``additionalUtxo``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address

from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import PoolEdit
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    PoolEditSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    build_pool_edit,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import edited_pool
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import LenderTerms
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import pool_min_ada
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios
from tests.lending.fluidtokens_v4.transactions.replay import second_pool

pytestmark = needs_ogmios

# Pool and manager spends and the four withdrawals.
_EDIT = sorted(["spend", "spend"] + ["withdraw"] * 4)


def _capture(name: str) -> tuple[dict, PoolEditSnapshot, PoolPosition]:
    fix = fixture(name)
    snapshot = PoolEditSnapshot.from_capture(fix)
    (captured,) = snapshot.positions
    return fix, snapshot, PoolPosition(captured.pool, captured.pool_manager)


@pytest.mark.parametrize("name", ["pool_edit", "pool_edit_deposit"])
def test_captured_edit_evaluates(name: str) -> None:
    fix, snapshot, _ = _capture(name)
    built = build(build_pool_edit, snapshot, slot=fix["invalid_before"])
    assert evaluate(built, fixture_utxos(fix)) == _EDIT


def test_new_terms_and_a_full_withdrawal_evaluate() -> None:
    fix, snapshot, position = _capture("pool_edit_deposit")
    terms = LenderTerms.from_pool_datum(position.pool_datum)
    floor = pool_min_ada(
        Address.decode(position.pool.address),
        position.pool_datum,
        pool_id=position.pool_id,
    )
    edit = PoolEdit(
        position.out_ref,
        terms=replace(
            terms,
            common_data=replace(terms.common_data, interest_rate=1_500),
        ),
        principal_change=floor - position.pool.lovelace,
    )
    built = build(
        build_pool_edit,
        replace(snapshot, positions=[edited_pool(position, edit)]),
        slot=fix["invalid_before"],
    )
    assert evaluate(built, fixture_utxos(fix)) == _EDIT


def test_two_pools_edited_together_evaluate() -> None:
    fix, snapshot, position = _capture("pool_edit")
    # The captured pool and its manager sit at 94087a91#0 and #1.
    other = second_pool(position, pool_ref=("95" * 32, 0), manager_ref=("95" * 32, 1))
    positions = [
        edited_pool(p, PoolEdit(p.out_ref, principal_change=1))
        for p in (position, other)
    ]
    built = build(
        build_pool_edit,
        replace(snapshot, positions=positions),
        slot=fix["invalid_before"],
    )
    utxos = fixture_utxos(fix) + [other.pool, other.pool_manager]
    assert evaluate(built, utxos) == sorted(_EDIT + ["spend", "spend"])
