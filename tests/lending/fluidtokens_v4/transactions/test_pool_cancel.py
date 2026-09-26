"""Offline: forward-built V4 pool cancels reproduce the captured mainnet cancel."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    PoolCancelSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    build_pool_cancel,
)
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import pool_fixture_backend
from tests.lending.fluidtokens_v4.transactions.replay import redeemers
from tests.lending.fluidtokens_v4.transactions.replay import second_pool

_OWNER = "1db7e8e3c128c1a3711c7326b232231c98441149041349ee8e0282ec"


def _capture() -> tuple[dict, PoolCancelSnapshot]:
    fix = fixture("pool_cancel")
    return fix, PoolCancelSnapshot.from_capture(fix)


def test_cancel_redeemers_are_byte_exact() -> None:
    fix, snapshot = _capture()
    built = build(build_pool_cancel, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)


def test_cancel_burns_both_nfts_and_needs_the_owner() -> None:
    fix, snapshot = _capture()
    body = build(
        build_pool_cancel, snapshot, slot=fix["invalid_before"]
    ).tx.transaction_body
    minted = sorted(
        [bytes(p).hex(), n.payload.hex(), q]
        for p, names in body.mint.items()
        for n, q in names.items()
    )
    assert minted == sorted([p, n, int(q)] for p, n, q in fix["mints"])
    assert [s.payload.hex() for s in body.required_signers] == [_OWNER]
    assert set(body.withdraws) == {
        reward_address(h)
        for h in (
            c.POOL_POLICY,
            c.POOL_MANAGER_POLICY,
            c.POOL_CANCEL_ACTION_SKH,
            c.POOL_MANAGER_CANCEL_ACTION_SKH,
        )
    }
    # The pool's value is left to the caller's change.
    assert not body.outputs


def test_cancel_records_the_funding_for_ogmios() -> None:
    fix, snapshot = _capture()
    build(build_pool_cancel, snapshot, slot=fix["invalid_before"])
    assert len(snapshot.additional_utxo()) == len(snapshot.funding) == 1


def test_the_burn_must_name_a_spent_input() -> None:
    fix, snapshot = _capture()
    with pytest.raises(ValueError, match="does not spend"):
        build(
            build_pool_cancel,
            replace(snapshot, mint_input_ref=("cc" * 32, 0)),
            slot=fix["invalid_before"],
        )


def test_a_cancel_is_the_only_pool_action() -> None:
    fix, snapshot = _capture()
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    tx_builder.withdrawals = {reward_address(c.POOL_MANAGER_POLICY): 0}  # type: ignore[assignment]
    with pytest.raises(ValueError, match="only pool action"):
        build_pool_cancel(tx_builder, snapshot=snapshot)


def test_a_pool_is_cancelled_once() -> None:
    fix, snapshot = _capture()
    (position,) = snapshot.positions
    with pytest.raises(ValueError, match="each pool once"):
        build(
            build_pool_cancel,
            replace(snapshot, positions=[position, position]),
            slot=fix["invalid_before"],
        )


def test_two_pools_whose_managers_sort_alike_cancel_together() -> None:
    fix, snapshot = _capture()
    (position,) = snapshot.positions
    other = second_pool(position, pool_ref=("70" * 32, 0), manager_ref=("52" * 32, 0))
    built = build(
        build_pool_cancel,
        replace(snapshot, positions=[other, position]),
        slot=fix["invalid_before"],
    )
    names = "581d" + "581d".join([position.pool_id.hex(), other.pool_id.hex()])
    cbors = {cbor for _, _, cbor in redeemers(built.tx)}
    # Four spends and two burns put the pool withdraw at 7; the names follow the
    # pools' input order.
    assert "d8799f0107ff" in cbors  # the pool-manager burn
    assert "d8799f01079f" + names + "ffff" in cbors  # the owner check


def test_from_backend_reproduces_the_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    fix, capture = _capture()
    pool_fixture_backend(monkeypatch, fix)
    snapshot = PoolCancelSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        pools=[capture.positions[0].out_ref],
        lender_address=capture.funding[0].address,
        funding=capture.funding,
    )
    # The burn names the first funding UTxO, as the captured cancel does.
    assert snapshot.mint_input_ref == capture.mint_input_ref
    built = build(build_pool_cancel, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)


def test_from_backend_without_funding_burns_against_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture()
    pool_fixture_backend(monkeypatch, fix)
    (position,) = capture.positions
    snapshot = PoolCancelSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        pools=[position.out_ref],
        lender_address=capture.funding[0].address,
        funding=[],
    )
    assert snapshot.mint_input_ref == position.out_ref
    build(build_pool_cancel, snapshot, slot=fix["invalid_before"])


def test_from_backend_refuses_a_pool_of_another_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture()
    pool_fixture_backend(monkeypatch, fix)
    stranger = fixture("pool_create")["outputs"][-1]["address"]
    with pytest.raises(ValueError, match="is owned by key"):
        PoolCancelSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            pools=[capture.positions[0].out_ref],
            lender_address=stranger,
            funding=[],
        )


def test_from_backend_refuses_a_wallet_with_no_funding_utxos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture()
    pool_fixture_backend(monkeypatch, fix)
    monkeypatch.setattr(resolve, "resolve_wallet_funding", lambda *_a, **_k: [])
    lender_address = capture.funding[0].address
    with pytest.raises(ValueError, match=f"no UTxOs at {lender_address} fund"):
        PoolCancelSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            pools=[capture.positions[0].out_ref],
            lender_address=lender_address,
        )


def test_from_backend_refuses_an_unknown_pool_manager_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture()
    pool_fixture_backend(monkeypatch, fix)
    config = resolve.config_datum
    monkeypatch.setattr(
        resolve,
        "config_datum",
        lambda utxo: replace(config(utxo), pool_manager_policy_id=b"\x01" * 28),
    )
    with pytest.raises(ValueError, match="owner checks are unknown"):
        PoolCancelSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            pools=[capture.positions[0].out_ref],
            lender_address=capture.funding[0].address,
            funding=[],
        )
