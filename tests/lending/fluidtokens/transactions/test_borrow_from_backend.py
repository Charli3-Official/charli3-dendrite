"""Offline: BorrowSnapshot.from_backend rebuilds the captured pool-origin borrow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIX = Path(__file__).parent / "fixtures"
_FILE = "borrow_pool.json"

# The captured borrow's derived collateral floor (principal 5_500_000_000; option
# divider 150 / min 100; oracle price 220485 / 100).
_MIN_COLLATERAL = 3_741_752


def _by_ref(fix: dict) -> dict:
    from charli3_dendrite.lending.fluidtokens.transactions.context import _as_utxo

    return {
        tuple(u["out_ref"]): _as_utxo(u)
        for u in fix["inputs"] + fix["ref_inputs"]
        if u.get("out_ref")
    }


def _patch_resolvers(monkeypatch, fix: dict, cap) -> None:  # noqa: ANN001
    by_ref = _by_ref(fix)

    def fake_by_outref(backend, h, i, *, allow_spent=False):  # noqa: ANN001, ANN202
        return by_ref[(h, i)]

    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_utxo_by_outref",
        fake_by_outref,
    )
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_config_utxo",
        lambda backend, *, allow_spent=False: cap.config,
    )
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_funding",
        lambda backend, address, **kw: cap.funding,
    )


def _from_backend(cap, **overrides):  # noqa: ANN001, ANN003, ANN202
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    kwargs = {
        "pool_utxo": cap.pool.out_ref,
        "borrower_address": cap.borrower_address,
        "principal_amount": cap.principal_amount,
        "chosen_collateral_index": cap.chosen_collateral_index,
        "oracle_reward_cbor": cap.oracle_reward_cbor,
        "oracle_feed_outref": cap.oracle_feed.out_ref,
        "oracle_script_ref_outref": cap.oracle_script_ref.out_ref,
        "fee_lovelace": cap.fee_lovelace,
        "collateral_amount": cap.collateral_amount,
        "valid_from": cap.valid_from,
        "valid_to": cap.valid_to,
        "allow_spent": True,
        "funding_outrefs": [u.out_ref for u in cap.funding],
        "config_outref": cap.config.out_ref,
        "pool_spend_ref_outref": cap.pool_spend_script_ref.out_ref,
        "pool_policy_ref_outref": cap.pool_policy_script_ref.out_ref,
        "loan_policy_ref_outref": cap.loan_policy_script_ref.out_ref,
        "lender_bond_policy_ref_outref": cap.lender_bond_policy_script_ref.out_ref,
        "borrower_bond_policy_ref_outref": cap.borrower_bond_policy_script_ref.out_ref,
    }
    kwargs.update(overrides)
    return BorrowSnapshot.from_backend(None, **kwargs)


def test_from_backend_matches_capture(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    _patch_resolvers(monkeypatch, fix, cap)

    snap = _from_backend(cap)

    assert snap.pool == cap.pool
    assert snap.config == cap.config
    assert snap.oracle_feed == cap.oracle_feed
    assert snap.pool_spend_script_ref == cap.pool_spend_script_ref
    assert snap.pool_policy_script_ref == cap.pool_policy_script_ref
    assert snap.loan_policy_script_ref == cap.loan_policy_script_ref
    assert snap.lender_bond_policy_script_ref == cap.lender_bond_policy_script_ref
    assert snap.borrower_bond_policy_script_ref == cap.borrower_bond_policy_script_ref
    assert snap.oracle_script_ref == cap.oracle_script_ref
    assert snap.oracle_reward_cbor == cap.oracle_reward_cbor
    assert snap.loan_id == cap.loan_id
    assert snap.pool_id == cap.pool_id
    assert snap.loan_address == cap.loan_address
    assert snap.collateral_unit == cap.collateral_unit
    assert snap.collateral_amount == cap.collateral_amount
    assert snap.pool_continuation_lovelace == cap.pool_continuation_lovelace
    assert snap.principal_amount == cap.principal_amount
    assert snap.chosen_collateral_index == cap.chosen_collateral_index
    assert snap.valid_from == cap.valid_from
    assert snap.valid_to == cap.valid_to
    assert snap.lender_bond_out.address == cap.lender_bond_out.address
    assert snap.lender_bond_out.datum == cap.lender_bond_out.datum
    assert snap.lender_bond_out.assets == cap.lender_bond_out.assets
    assert snap.fee_address == cap.fee_address
    assert snap.fee_lovelace == cap.fee_lovelace
    assert snap.loan_policy == cap.loan_policy
    assert snap.pool_policy == cap.pool_policy
    assert snap.lender_bond_policy == cap.lender_bond_policy
    assert snap.borrower_bond_policy == cap.borrower_bond_policy


def test_min_collateral_helper_and_default(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        BorrowSnapshot,
        _min_collateral_amount,
    )
    from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    oracle = OracleReward.parse(cap.oracle_reward_cbor)

    assert (
        _min_collateral_amount(
            cap.pool_datum,
            chosen_collateral_index=cap.chosen_collateral_index,
            principal_amount=cap.principal_amount,
            price_num=oracle.price_num,
            price_den=oracle.price_den,
        )
        == _MIN_COLLATERAL
    )

    _patch_resolvers(monkeypatch, fix, cap)
    snap = _from_backend(cap, collateral_amount=None)
    assert snap.collateral_amount == _MIN_COLLATERAL


def test_from_backend_rejects_undersized_collateral(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    _patch_resolvers(monkeypatch, fix, cap)

    with pytest.raises(ValueError, match="below the minimum"):
        _from_backend(cap, collateral_amount=_MIN_COLLATERAL - 1)


def test_from_backend_rejects_malformed_witness(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    _patch_resolvers(monkeypatch, fix, cap)

    with pytest.raises(ValueError, match="oracle reward"):
        _from_backend(cap, oracle_reward_cbor="deadbeef")


def test_from_backend_requires_oracle_script_ref(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    _patch_resolvers(monkeypatch, fix, cap)

    with pytest.raises(ValueError, match="oracle_script_ref_outref"):
        _from_backend(cap, oracle_script_ref_outref=None)


def test_from_backend_rejects_mismatched_lender_bond_datum(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import BorrowSnapshot

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    _patch_resolvers(monkeypatch, fix, cap)

    # A well-formed but wrong preimage: its blake2b_256 does not match the pool commit.
    with pytest.raises(ValueError, match="does not hash to the pool"):
        _from_backend(cap, lender_bond_datum="d87a80")


def test_resolve_lender_bond_datum_requires_preimage_for_non_unit_commit() -> None:
    from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        BorrowSnapshot,
        _resolve_lender_bond_datum,
    )

    fix = json.loads((_FIX / _FILE).read_text())
    cap = BorrowSnapshot.from_capture(fix)
    pool_datum = PoolDatum.from_cbor(bytes.fromhex(cap.pool.datum))
    # Commit a non-Unit hash: the Unit-datum default no longer matches, so omitting the
    # preimage must fail loudly rather than emit a wrong lender-bond datum.
    pool_datum.lender_bond_inline_datum_hash = b"\x00" * 32

    with pytest.raises(ValueError, match="lender_bond_datum is required"):
        _resolve_lender_bond_datum(pool_datum, None)
