"""Offline structural test for the forward create-loan builder.

`build_create_loan` contributes a create-loan transaction to a caller-supplied
`pycardano.TransactionBuilder`, mirroring the DEX seam: a pool script input, the
config/market/oracle reference inputs, the loan-token + owner-NFT mint, the oracle
withdraw-zero redeemer, and the updated-pool + loan outputs. The caller owns the
chain context, balancing, and evaluation, so this test only asserts that the
builder ends up WIRED (the synthesized redeemers/prices/indices are proven on
Ogmios in the balancing task). It runs fully offline against a network-free chain
context and a `CreateLoanSnapshot` reconstructed from the captured fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash
from pycardano import TransactionBuilder

from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.oracles.aggregator_datums import (
    OracleGlobalConfig,
)
from charli3_dendrite.lending.danogo.oracles.aggregator_datums import OraclePathDatum
from charli3_dendrite.lending.danogo.transactions.build import build_create_loan
from charli3_dendrite.lending.danogo.transactions.context import LOAN_MINT_SCRIPT_SKH
from charli3_dendrite.lending.danogo.transactions.context import ORACLE_SKH
from charli3_dendrite.lending.danogo.transactions.context import POOL_SCRIPT_SKH
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanContext
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import _as_utxo
from charli3_dendrite.lending.danogo.transactions.loan_ids import owner_nft_name

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text(),
)


@pytest.fixture
def snap(script_hash, parses) -> CreateLoanSnapshot:  # noqa: ANN001
    """Reconstruct a `CreateLoanSnapshot` from the captured create-loan fixture.

    The fixture is a post-state tx, so the snapshot's read-side UTxOs are classified
    back out of it: the protocol-config (config NFT), market + pool (config-pool NFT),
    the three published reference scripts (by script hash), the Danogo oracle data
    UTxOs (global config + price-path datums), and the remaining external price-source
    leaves.
    """
    ctx = CreateLoanContext.from_fixture(FIX)
    ref_inputs = [_as_utxo(u) for u in FIX["ref_inputs"]]
    inputs = [_as_utxo(u) for u in FIX["inputs"]]

    protocol_config = ref_inputs[ctx.protocol_cfg_ref_idx]
    pd = ProtocolDatum.from_cbor(protocol_config.datum)
    config_pool_skh = pd.config_pool_skh.hex()

    market = ref_inputs[ctx.market_ref_idx]
    market_info = DanogoMarket.from_market_datum(market.datum)

    pool = next(u for u in inputs if u.out_ref == tuple(ctx.pool_in_out_ref))

    by_hash = {script_hash(u): u for u in ref_inputs if u.ref_script}
    pool_script_ref = by_hash[POOL_SCRIPT_SKH]
    loan_mint_script_ref = by_hash[LOAN_MINT_SCRIPT_SKH]
    oracle_script_ref = by_hash[ORACLE_SKH]
    script_ref_refs = set(id(u) for u in by_hash.values())

    oracle_data_refs = [
        u
        for u in ref_inputs
        if parses(u.datum, OraclePathDatum) or parses(u.datum, OracleGlobalConfig)
    ]

    classified = {id(protocol_config), id(market)}
    classified |= script_ref_refs
    classified |= {id(u) for u in oracle_data_refs}
    oracle_source_leaves = [u for u in ref_inputs if id(u) not in classified]

    return CreateLoanSnapshot(
        market_name=ctx.market_name,
        loan_skh=ctx.loan_skh,
        pool_skh=pd.pool_skh.hex(),
        config_pool_skh=config_pool_skh,
        oracle_skh=pd.oracle_skh.hex(),
        protocol_config=protocol_config,
        market=market,
        market_info=market_info,
        pool=pool,
        oracle_data_refs=oracle_data_refs,
        pool_script_ref=pool_script_ref,
        loan_mint_script_ref=loan_mint_script_ref,
        oracle_script_ref=oracle_script_ref,
        oracle_source_leaves=oracle_source_leaves,
    )


def test_build_wires_pool_input_mint_outputs_and_oracle_withdrawal(
    offline_ctx,  # noqa: ANN001
    actor_addr,  # noqa: ANN001
    snap,  # noqa: ANN001
):
    collateral_unit = "94dca24a1f1fcc2ff51cd90f32f4fe9e786d861a2dbf7d27598d26e8"
    collateral_unit += "251d5ccb51543f3647b344d4a4c8f2df5bff9d164854e3e7fe4b1711"

    tx_builder = TransactionBuilder(offline_ctx)
    build_create_loan(
        tx_builder,
        snapshot=snap,
        actor_address=actor_addr,
        collateral={collateral_unit: 1_030_413_025},
        borrow_amount=9_000_000,
    )

    assert len(tx_builder.inputs) >= 1  # pool spend
    assert len(tx_builder.outputs) >= 2  # updated pool + loan utxo
    assert tx_builder.mint is not None  # loan token + owner NFT
    assert len(tx_builder.reference_inputs) >= 3  # protocol cfg, market, oracle data


def test_mint_carries_loan_token_and_owner_nft(
    offline_ctx, actor_addr, snap
):  # noqa: ANN001
    tx_builder = TransactionBuilder(offline_ctx)
    build_create_loan(
        tx_builder,
        snapshot=snap,
        actor_address=actor_addr,
        collateral={},
        borrow_amount=9_000_000,
    )

    minted = {
        bytes(policy).hex() + bytes(name).hex(): qty
        for policy, names in tx_builder.mint.data.items()
        for name, qty in names.items()
    }
    loan_token = snap.loan_skh + snap.market_name
    assert minted.get(loan_token) == 1
    # The owner NFT name is the shared blake2b-224 of the spent pool's tx id.
    owner_unit = snap.loan_skh + owner_nft_name(snap.pool.out_ref)
    assert minted.get(owner_unit) == 1


def test_oracle_withdrawal_is_wired(offline_ctx, actor_addr, snap):  # noqa: ANN001
    tx_builder = TransactionBuilder(offline_ctx)
    build_create_loan(
        tx_builder,
        snapshot=snap,
        actor_address=actor_addr,
        collateral={},
        borrow_amount=9_000_000,
    )

    assert tx_builder.withdrawals is not None
    reward = Address(
        staking_part=ScriptHash(bytes.fromhex(snap.oracle_skh)),
        network=Network.MAINNET,
    )
    withdrawals = tx_builder.withdrawals.to_primitive()
    assert bytes(reward) in withdrawals
    assert withdrawals[bytes(reward)] == 0


def test_validity_window_within_create_loan_bound(
    offline_ctx, actor_addr, snap
):  # noqa: ANN001
    tx_builder = TransactionBuilder(offline_ctx)
    build_create_loan(
        tx_builder,
        snapshot=snap,
        actor_address=actor_addr,
        collateral={},
        borrow_amount=9_000_000,
    )

    assert tx_builder.validity_start is not None
    assert tx_builder.ttl is not None
    assert (tx_builder.ttl - tx_builder.validity_start) * 1000 <= 360_000
