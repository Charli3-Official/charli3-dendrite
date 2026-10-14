"""V4 transaction plumbing: addresses, withdrawals, NFT names and minimum ADA."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address
from pycardano import RawCBOR
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import build_borrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    build_change_collateral,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import ClaimSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import build_claim
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    add_zero_withdrawals,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    captured_config,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import loan_address
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import pool_nft_name
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    settled_min_ada,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    PoolCancelSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    build_pool_cancel,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    PoolEditSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    build_pool_edit,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import build_repay
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import captured_scripts
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import redeemers

# The action hashes the config datum names; a capture must not depend on them.
_ACTION_CONSTANTS = [
    "LOAN_CLAIM_ACTION_SKH",
    "LOAN_REPAY_ACTION_SKH",
    "LOAN_CHANGE_COLLATERAL_ACTION_SKH",
    "LOAN_RECAST_ACTION_SKH",
    "POOL_CANCEL_ACTION_SKH",
    "POOL_BORROW_ACTION_SKH",
    "POOL_SELL_LENDER_POSITION_ACTION_SKH",
    "POOL_COMPOUND_ACTION_SKH",
    "POOL_EDIT_ACTION_SKH",
]


def test_loan_address_carries_the_borrower_stake() -> None:
    for name in ("borrow_single", "borrow_multi"):
        fix = fixture(name)
        loans = [
            u
            for u in fix["outputs"]
            if any(p == c.LOAN_POLICY for p, _, _ in u["assets"])
        ]
        wallet = next(u["address"] for u in fix["inputs"] if not u.get("datum"))
        assert loans
        assert {u["address"] for u in loans} == {loan_address(wallet)}


def test_loan_address_of_an_enterprise_wallet_has_no_stake() -> None:
    fix = fixture("borrow_single")
    base = Address.decode(
        next(u["address"] for u in fix["inputs"] if not u.get("datum"))
    )
    wallet = Address(payment_part=base.payment_part, network=base.network).encode()
    loan = Address.decode(loan_address(wallet))
    assert loan.staking_part is None
    assert loan.payment_part.payload.hex() == c.LOAN_SPEND_SKH


def test_zero_withdrawals_merge_with_existing_ones() -> None:
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=0))
    add_zero_withdrawals(tx_builder, [c.POOL_POLICY])
    add_zero_withdrawals(tx_builder, [c.LOAN_POLICY])
    assert dict(tx_builder.withdrawals) == {
        reward_address(c.POOL_POLICY): 0,
        reward_address(c.LOAN_POLICY): 0,
    }


def test_script_hash_of_a_reference_script() -> None:
    fix = fixture("borrow_single")
    refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
    hashes = {script_hash_of(u) for u in refs if u.ref_script}
    borrow_action = captured_scripts(fix).pool_borrow_action_script_hash.hex()
    assert {c.POOL_SPEND_SKH, c.POOL_POLICY, borrow_action} <= hashes


@pytest.mark.parametrize(
    ("name", "out_ref"),
    [
        (
            "borrow_single",
            "454814391c3a399c3dbdffb208fee60442f12890dc2dd002b1b53561cecb5eba",
        ),
        (
            "borrow_single_oracle_v2",
            "3d800e98a4da21dc9abcce30c145729406fef7db4d5cd3b4ecd6813aa228a75c",
        ),
    ],
)
def test_captured_config_is_the_config_the_capture_referenced(
    name: str,
    out_ref: str,
) -> None:
    refs = [utxo_from_dict(u) for u in fixture(name)["ref_inputs"]]
    config, scripts = captured_config(refs)
    assert config.out_ref == (out_ref, 0)
    assert scripts == ConfigDatum.from_cbor(config.datum)


def test_captured_config_needs_the_config_reference_input() -> None:
    refs = [utxo_from_dict(u) for u in fixture("borrow_single")["ref_inputs"]]
    config, _ = captured_config(refs)
    with pytest.raises(ValueError, match="no config reference input"):
        captured_config([u for u in refs if u.out_ref != config.out_ref])


@pytest.mark.parametrize(
    ("name", "snapshot_cls", "contribute"),
    [
        ("borrow_single", BorrowSnapshot, build_borrow),
        ("borrow_multi", BorrowSnapshot, build_borrow),
        ("borrow_single_oracle_v2", BorrowSnapshot, build_borrow),
        ("borrow_two_oracle_v2", BorrowSnapshot, build_borrow),
        ("repay_single", RepaySnapshot, build_repay),
        ("change_collateral_single", ChangeCollateralSnapshot, build_change_collateral),
        ("pool_edit", PoolEditSnapshot, build_pool_edit),
        ("pool_cancel", PoolCancelSnapshot, build_pool_cancel),
        ("pool_cancel_after_config_update", PoolCancelSnapshot, build_pool_cancel),
        ("claim", ClaimSnapshot, build_claim),
    ],
)
def test_a_capture_replays_under_the_config_it_referenced(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    snapshot_cls: type,
    contribute: object,
) -> None:
    # Captures span config updates: their action scripts come from the config they
    # referenced, whatever this release's constants name.
    for constant in _ACTION_CONSTANTS:
        monkeypatch.setattr(c, constant, "00" * 28)
    fix = fixture(name)
    snapshot = snapshot_cls.from_capture(fix)  # type: ignore[attr-defined]
    built = build(contribute, snapshot, slot=fix["invalid_before"])  # type: ignore[arg-type]
    assert redeemers(built.tx) == captured_redeemers(fix)


def test_settled_min_ada_is_what_a_pool_manager_holds() -> None:
    # Every captured pool manager holds exactly the least ADA its output needs.
    for name in ("pool_create", "pool_create_token", "pool_cancel"):
        fix = fixture(name)
        manager = next(
            utxo_from_dict(u)
            for u in fix["inputs"] + fix["outputs"]
            if any(p == c.POOL_MANAGER_POLICY for p, _, _ in u["assets"])
        )
        assets = {p + n: q for p, n, q in manager.assets}
        datum = RawCBOR(bytes.fromhex(manager.datum))
        address = Address.decode(manager.address)
        assert settled_min_ada(address, assets, datum) == manager.lovelace == 1_456_780


def test_pool_nft_name_needs_exactly_one_pool_nft() -> None:
    fix = fixture("pool_cancel")
    pool = next(
        utxo_from_dict(u)
        for u in fix["inputs"]
        if any(p == c.POOL_POLICY for p, _, _ in u["assets"])
    )
    assert pool_nft_name(pool).hex().startswith("0024af38")
    with pytest.raises(ValueError, match="exactly one pool NFT"):
        pool_nft_name(replace(pool, assets=[]))
    with pytest.raises(ValueError, match="exactly one pool NFT"):
        pool_nft_name(replace(pool, assets=pool.assets + [(c.POOL_POLICY, "01", 1)]))
