"""Offline: forward-built V4 pool edits reproduce the captured mainnet edits."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address

from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import Asset
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import EditedPool
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import PoolEdit
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    PoolEditSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    build_pool_edit,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import edited_pool
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import (
    LenderTerms,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import (
    pool_min_ada,
)
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_output_view
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import output_view
from tests.lending.fluidtokens_v4.transactions.replay import pool_fixture_backend
from tests.lending.fluidtokens_v4.transactions.replay import redeemers

CAPTURES = ["pool_edit", "pool_edit_deposit"]


def _capture(name: str) -> tuple[dict, PoolEditSnapshot]:
    fix = fixture(name)
    return fix, PoolEditSnapshot.from_capture(fix)


def _edit(name: str) -> PoolEdit:
    """The captured edit as terms and a principal change."""
    fix, snapshot = _capture(name)
    (position,) = snapshot.positions
    after = utxo_from_dict(fix["outputs"][0])
    datum = PoolDatum.from_cbor(after.datum)
    unit = datum.common_data.principal_asset.unit()

    def held(utxo: object) -> int:
        if unit == "lovelace":
            return utxo.lovelace  # type: ignore[attr-defined]
        return next((q for p, n, q in utxo.assets if p + n == unit), 0)  # type: ignore[attr-defined]

    return PoolEdit(
        position.out_ref,
        terms=LenderTerms.from_pool_datum(datum),
        principal_change=held(after) - held(position.pool),
    )


@pytest.mark.parametrize("name", CAPTURES)
def test_edit_redeemers_and_outputs_are_byte_exact(name: str) -> None:
    fix, snapshot = _capture(name)
    built = build(build_pool_edit, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)
    body = built.tx.transaction_body
    # The continuing pool, then its unchanged pool manager.
    assert [output_view(o) for o in body.outputs] == [
        captured_output_view(o) for o in fix["outputs"][:2]
    ]
    assert body.mint is None
    assert set(body.withdraws) == {
        reward_address(h)
        for h in (
            c.POOL_POLICY,
            c.POOL_MANAGER_POLICY,
            c.POOL_EDIT_ACTION_SKH,
            c.POOL_MANAGER_EDIT_POOL_ACTION_SKH,
        )
    }


@pytest.mark.parametrize(
    ("name", "change"),
    [("pool_edit", 0), ("pool_edit_deposit", 10_000_000)],
)
def test_terms_and_principal_change_reproduce_the_capture(
    name: str,
    change: int,
) -> None:
    fix, snapshot = _capture(name)
    edit = _edit(name)
    assert edit.principal_change == change
    (position,) = snapshot.positions
    edited = edited_pool(PoolPosition(position.pool, position.pool_manager), edit)
    built = build(
        build_pool_edit,
        replace(snapshot, positions=[edited]),
        slot=fix["invalid_before"],
    )
    assert redeemers(built.tx) == captured_redeemers(fix)
    assert [output_view(o) for o in built.tx.transaction_body.outputs] == [
        captured_output_view(o) for o in fix["outputs"][:2]
    ]


def test_from_backend_reproduces_the_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    fix, capture = _capture("pool_edit_deposit")
    pool_fixture_backend(monkeypatch, fix)
    snapshot = PoolEditSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        edits=[_edit("pool_edit_deposit")],
        lender_address=capture.funding[0].address,
        funding=capture.funding,
    )
    built = build(build_pool_edit, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)


def _position(name: str) -> PoolPosition:
    (position,) = _capture(name)[1].positions
    return PoolPosition(position.pool, position.pool_manager)


def test_no_terms_keep_the_datum_bytes() -> None:
    position = _position("pool_edit")
    edited = edited_pool(position, PoolEdit(position.out_ref, principal_change=5))
    assert edited.datum == position.pool.datum
    unit = position.pool_datum.common_data.principal_asset.unit()
    assert (unit[:56], unit[56:], 188_000_005) in edited.assets


def test_an_edit_keeps_the_principal_and_its_oracle() -> None:
    position = _position("pool_edit")
    terms = LenderTerms.from_pool_datum(position.pool_datum)
    other = Asset(policy_id=b"\x01" * 28, asset_name=b"")
    for field in ("principal_asset", "principal_oracle_asset"):
        changed = replace(
            terms, common_data=replace(terms.common_data, **{field: other})
        )
        with pytest.raises(ValueError, match="principal or its oracle"):
            edited_pool(position, PoolEdit(position.out_ref, terms=changed))


def test_an_ada_pool_releases_at_most_what_it_holds_above_its_minimum() -> None:
    position = _position("pool_edit_deposit")
    pool = position.pool
    floor = pool_min_ada(
        Address.decode(pool.address),
        position.pool_datum,
        pool_id=position.pool_id,
    )
    most = pool.lovelace - floor
    edited = edited_pool(position, PoolEdit(position.out_ref, principal_change=-most))
    assert edited.lovelace == floor
    with pytest.raises(ValueError, match=f"can release at most {most} lovelace"):
        edited_pool(position, PoolEdit(position.out_ref, principal_change=-most - 1))


def test_a_token_pool_releases_at_most_its_principal() -> None:
    position = _position("pool_edit")
    edited = edited_pool(
        position, PoolEdit(position.out_ref, principal_change=-188_000_000)
    )
    assert [p for p, _, _ in edited.assets] == [c.POOL_POLICY]
    assert edited.lovelace == position.pool.lovelace
    with pytest.raises(ValueError, match="holds 188000000 of its principal"):
        edited_pool(position, PoolEdit(position.out_ref, principal_change=-188_000_001))


def test_a_token_pool_is_topped_up_when_its_datum_grows() -> None:
    position = _position("pool_edit")
    terms = LenderTerms.from_pool_datum(position.pool_datum)
    options = list(terms.collateral_options) * 4
    grown = replace(
        terms,
        collateral_options=options,
        min_collateral=[100] * len(options),
        min_collateral_divider=[150] * len(options),
    )
    edited = edited_pool(position, PoolEdit(position.out_ref, terms=grown))
    assert edited.lovelace > position.pool.lovelace
    assert edited.lovelace == pool_min_ada(
        Address.decode(position.pool.address),
        PoolDatum.from_cbor(edited.datum),
        pool_id=position.pool_id,
    )


def test_an_ada_pool_is_topped_up_when_its_datum_grows() -> None:
    position = _position("pool_edit_deposit")
    floor = pool_min_ada(
        Address.decode(position.pool.address),
        position.pool_datum,
        pool_id=position.pool_id,
    )
    # Fully lent out: the pool holds only the ADA it must keep.
    drained = PoolPosition(
        replace(position.pool, lovelace=floor), position.pool_manager
    )
    terms = LenderTerms.from_pool_datum(position.pool_datum)
    options = list(terms.collateral_options) * 3
    grown = replace(
        terms,
        collateral_options=options,
        min_collateral=[100] * len(options),
        min_collateral_divider=[150] * len(options),
    )
    edited = edited_pool(drained, PoolEdit(position.out_ref, terms=grown))
    assert edited.lovelace == pool_min_ada(
        Address.decode(position.pool.address),
        PoolDatum.from_cbor(edited.datum),
        pool_id=position.pool_id,
    )
    assert edited.lovelace > floor


def test_an_edit_refuses_terms_no_borrower_could_take_up() -> None:
    position = _position("pool_edit")
    terms = replace(
        LenderTerms.from_pool_datum(position.pool_datum),
        min_collateral_divider=[0, 0],
    )
    with pytest.raises(ValueError, match="positive"):
        edited_pool(position, PoolEdit(position.out_ref, terms=terms))


def test_a_continuing_manager_keeps_its_reference_script() -> None:
    from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
        _continuing_manager,
    )

    (position,) = _capture("pool_edit")[1].positions
    ref_inputs = [utxo_from_dict(u) for u in fixture("pool_create")["ref_inputs"]]
    script = script_ref_by_hash(ref_inputs, c.POOL_MANAGER_POLICY)
    manager = replace(
        position.pool_manager,
        ref_script=script.ref_script,
        ref_script_type=script.ref_script_type,
    )
    edited = replace(position, pool_manager=manager)
    output = _continuing_manager(edited)
    assert output.script is not None
    assert bytes(output.script).hex() == script.ref_script


def test_a_continuing_pool_below_its_minimum_is_refused() -> None:
    fix, snapshot = _capture("pool_edit")
    (position,) = snapshot.positions
    starved = EditedPool(
        pool=position.pool,
        pool_manager=position.pool_manager,
        datum=position.datum,
        lovelace=1_000_000,
        assets=position.assets,
    )
    with pytest.raises(ValueError, match="below the"):
        build(build_pool_edit, replace(snapshot, positions=[starved]), slot=0)
