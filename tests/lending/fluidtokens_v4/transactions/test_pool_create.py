"""Offline: forward-built V4 pool creates reproduce the captured mainnet creates."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    pool_address,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_create import PoolOffer
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_create import (
    PoolCreateSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_create import (
    build_pool_create,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_create import (
    new_pool_id,
)
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_output_view
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import output_view
from tests.lending.fluidtokens_v4.transactions.replay import pool_fixture_backend
from tests.lending.fluidtokens_v4.transactions.replay import redeemers

CAPTURES = ["pool_create", "pool_create_token"]


def _capture(name: str) -> tuple[dict, PoolCreateSnapshot]:
    fix = fixture(name)
    return fix, PoolCreateSnapshot.from_capture(fix)


@pytest.mark.parametrize("name", CAPTURES)
def test_create_redeemers_outputs_and_mints_are_byte_exact(name: str) -> None:
    fix, snapshot = _capture(name)
    body = build(build_pool_create, snapshot, slot=fix["invalid_before"]).tx
    assert redeemers(body) == captured_redeemers(fix)
    outputs = body.transaction_body.outputs
    # The pool, with the datum built from the lender's terms, then its manager.
    assert [output_view(o) for o in outputs] == [
        captured_output_view(o) for o in fix["outputs"][:2]
    ]
    minted = sorted(
        [bytes(p).hex(), n.payload.hex(), q]
        for p, names in body.transaction_body.mint.items()
        for n, q in names.items()
    )
    assert minted == sorted([p, n, int(q)] for p, n, q in fix["mints"])


def test_the_names_derive_from_the_input_ref() -> None:
    _, snapshot = _capture("pool_create")
    # The captured create's names come from its third input in ledger order.
    assert snapshot.input_ref == (
        "4affe3d25bc1c677bea4a143df14ffc6e3c5cf48c5b6628823a0ed5dc753f4cf",
        0,
    )
    assert snapshot.pool_ids == [
        bytes.fromhex("005c91f022ef7a05462651a64cd80c9601c52ba595f0513afc58736290"),
    ]


def test_create_records_the_funding_for_ogmios() -> None:
    fix, snapshot = _capture("pool_create")
    build(build_pool_create, snapshot, slot=fix["invalid_before"])
    assert len(snapshot.additional_utxo()) == len(snapshot.funding) == 12


def test_several_pools_get_indexed_names_and_their_own_commitments() -> None:
    fix, snapshot = _capture("pool_create")
    (offer,) = snapshot.offers
    three = replace(snapshot, offers=[offer, replace(offer, principal_amount=1), offer])
    outputs = build(build_pool_create, three, slot=fix["invalid_before"]).tx
    outputs = outputs.transaction_body.outputs
    suffix = snapshot.pool_ids[0][1:]
    assert three.pool_ids == [bytes([k]) + suffix for k in range(3)]
    pools = outputs[0::2]
    managers = outputs[1::2]
    assert [
        next(
            iter(o.amount.multi_asset[ScriptHash(bytes.fromhex(c.POOL_POLICY))])
        ).payload
        for o in pools
    ] == three.pool_ids
    assert [
        next(
            iter(
                o.amount.multi_asset[ScriptHash(bytes.fromhex(c.POOL_MANAGER_POLICY))]
            ),
        ).payload
        for o in managers
    ] == three.pool_ids
    hashes = {
        bytes(three.pool_datum(k).lender_bond_inline_datum_hash) for k in range(3)
    }
    assert len(hashes) == 3  # noqa: PLR2004


def test_from_backend_keeps_only_the_least_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture("pool_create")
    pool_fixture_backend(monkeypatch, fix)
    (offer,) = capture.offers
    snapshot = PoolCreateSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        offers=[PoolOffer(offer.terms, 11_400_000_000)],
        lender_address=capture.lender_address,
        funding=capture.funding,
        input_ref=capture.input_ref,
    )
    built = build(build_pool_create, snapshot, slot=fix["invalid_before"]).tx
    assert redeemers(built) == captured_redeemers(fix)
    pool, manager = built.transaction_body.outputs
    captured_pool, captured_manager = (
        captured_output_view(o) for o in fix["outputs"][:2]
    )
    # The pool lends its 11,400 ADA with the least reserve, not a flat 4.5 ADA.
    assert output_view(pool)[1] < captured_pool[1]
    assert output_view(pool)[3] == captured_pool[3]
    assert output_view(manager) == captured_manager


def test_from_backend_names_pools_after_the_first_funding_utxo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture("pool_create")
    pool_fixture_backend(monkeypatch, fix)
    snapshot = PoolCreateSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        offers=capture.offers,
        lender_address=capture.lender_address,
        funding=capture.funding,
    )
    first = min(
        capture.funding, key=lambda u: (bytes.fromhex(u.out_ref[0]), u.out_ref[1])
    )
    assert snapshot.input_ref == first.out_ref
    assert snapshot.pool_ids == [new_pool_id(first.out_ref, 0)]


def test_a_reserve_below_the_pools_minimum_is_refused() -> None:
    fix, snapshot = _capture("pool_create_token")
    (offer,) = snapshot.offers
    starved = replace(snapshot, offers=[replace(offer, reserve_lovelace=2_000_000)])
    with pytest.raises(ValueError, match="below the"):
        build(build_pool_create, starved, slot=fix["invalid_before"])


def test_a_pool_lends_a_positive_principal() -> None:
    fix, snapshot = _capture("pool_create")
    (offer,) = snapshot.offers
    with pytest.raises(ValueError, match="positive principal"):
        build(
            build_pool_create,
            replace(snapshot, offers=[replace(offer, principal_amount=0)]),
            slot=fix["invalid_before"],
        )


def test_a_create_needs_offers_and_a_key_lender() -> None:
    fix, snapshot = _capture("pool_create")
    with pytest.raises(ValueError, match="at least one offer"):
        build(build_pool_create, replace(snapshot, offers=[]), slot=0)
    script_lender = str(pool_address(snapshot.lender_address))
    with pytest.raises(NotImplementedError, match="key address"):
        build(
            build_pool_create, replace(snapshot, lender_address=script_lender), slot=0
        )


@pytest.mark.parametrize(
    "fees",
    [{"compounding_fee_per_mille": -1}, {"liquidation_fee_per_mille": 1001}],
)
def test_fees_are_per_mille(fees: dict) -> None:
    _, snapshot = _capture("pool_create")
    with pytest.raises(ValueError, match="0 to 1000"):
        build(build_pool_create, replace(snapshot, **fees), slot=0)


def test_the_names_must_derive_from_a_spent_input() -> None:
    fix, snapshot = _capture("pool_create")
    with pytest.raises(ValueError, match="does not spend"):
        build(
            build_pool_create,
            replace(snapshot, input_ref=("cc" * 32, 0)),
            slot=fix["invalid_before"],
        )


def test_a_create_is_the_only_pool_action() -> None:
    fix, snapshot = _capture("pool_create")
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    tx_builder.add_output(
        TransactionOutput(pool_address(snapshot.lender_address), 2_000_000),
    )
    with pytest.raises(ValueError, match="only pool action"):
        build_pool_create(tx_builder, snapshot=snapshot)


def test_from_backend_refuses_explicit_empty_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture("pool_create")
    pool_fixture_backend(monkeypatch, fix)
    with pytest.raises(ValueError, match="the create needs a funding UTxO"):
        PoolCreateSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            offers=capture.offers,
            lender_address=capture.lender_address,
            funding=[],
        )


def test_from_backend_refuses_a_wallet_with_no_funding_utxos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture("pool_create")
    pool_fixture_backend(monkeypatch, fix)
    monkeypatch.setattr(resolve, "resolve_wallet_funding", lambda *_a, **_k: [])
    with pytest.raises(
        ValueError,
        match=f"no UTxOs at {capture.lender_address} fund the create",
    ):
        PoolCreateSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            offers=capture.offers,
            lender_address=capture.lender_address,
        )


def test_from_backend_refuses_an_unknown_pool_manager_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture("pool_create")
    pool_fixture_backend(monkeypatch, fix)
    config = resolve.config_datum
    monkeypatch.setattr(
        resolve,
        "config_datum",
        lambda utxo: replace(config(utxo), pool_manager_policy_id=b"\x01" * 28),
    )
    with pytest.raises(ValueError, match="owner checks are unknown"):
        PoolCreateSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            offers=capture.offers,
            lender_address=capture.lender_address,
            funding=capture.funding,
        )


def test_the_pools_are_staked_by_the_lender() -> None:
    _, snapshot = _capture("pool_create")
    lender = Address.decode(snapshot.lender_address)
    pool = snapshot.pool_output(0)
    manager = snapshot.pool_manager_output(0)
    assert (
        pool.address.staking_part == manager.address.staking_part == lender.staking_part
    )
    assert manager.address.payment_part.payload.hex() == c.POOL_MANAGER_SPEND_SKH
