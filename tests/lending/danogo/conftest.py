"""Shared offline-build test helpers for the Danogo transaction-builder tests.

The structural builder tests (`test_build_create_loan_forward`,
`test_build_topup_withdraw`) assemble script inputs, mints, and outputs against a
network-free chain context and snapshots reconstructed from captured fixtures --
they never balance, sign, or evaluate. The offline chain context, placeholder
actor address, reference-script hash helper, and "does this datum parse?" probe
are identical across those suites, so they live here and are exposed as pytest
fixtures (`offline_ctx`, `actor_addr`, `script_hash`, `parses`) that pytest
auto-discovers for every test in this directory -- no cross-module import needed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import PlutusData
from pycardano import PlutusV3Script
from pycardano import ProtocolParameters
from pycardano import VerificationKeyHash
from pycardano import plutus_script_hash
from pycardano.backend.base import ChainContext

from charli3_dendrite.lending.danogo.constants import PROTOCOL_CONFIG_NFT
from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.oracles.deployments import (
    deployment_for_oracle_skh,
)
from charli3_dendrite.lending.danogo.transactions.context import LOAN_MINT_SCRIPT_SKH
from charli3_dendrite.lending.danogo.transactions.context import POOL_SCRIPT_SKH
from charli3_dendrite.lending.danogo.transactions.context import IncreaseLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import (
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.danogo.transactions.context import Utxo
from charli3_dendrite.lending.danogo.transactions.context import _as_utxo

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _OfflineContext(ChainContext):
    """A network-free chain context: enough for `min_lovelace` + builder mutation.

    The structural builder tests only assemble script inputs, mints, and outputs;
    they never balance, sign, or evaluate, so the protocol parameters just need to
    satisfy `min_lovelace_post_alonzo` (`coins_per_utxo_byte`).
    """

    @property
    def protocol_param(self) -> ProtocolParameters:
        return ProtocolParameters(
            min_fee_constant=155381,
            min_fee_coefficient=44,
            max_block_size=98304,
            max_tx_size=16384,
            max_block_header_size=1100,
            key_deposit=2000000,
            pool_deposit=500000000,
            pool_influence=0.3,
            monetary_expansion=0.003,
            treasury_expansion=0.2,
            decentralization_param=0,
            extra_entropy="",
            protocol_major_version=8,
            protocol_minor_version=0,
            min_utxo=1000000,
            min_pool_cost=340000000,
            price_mem=0.0577,
            price_step=0.0000721,
            max_tx_ex_mem=14000000,
            max_tx_ex_steps=10000000000,
            max_block_ex_mem=62000000,
            max_block_ex_steps=20000000000,
            max_val_size=5000,
            collateral_percent=150,
            max_collateral_inputs=3,
            coins_per_utxo_word=34482,
            coins_per_utxo_byte=4310,
            cost_models={},
        )

    @property
    def genesis_param(self):  # noqa: ANN201
        return None

    @property
    def network(self) -> Network:
        return Network.MAINNET

    @property
    def epoch(self) -> int:
        return 0

    @property
    def last_block_slot(self) -> int:
        return 190412105

    def utxos(self, address):  # noqa: ANN001, ANN201
        return []

    def submit_tx_cbor(self, cbor):  # noqa: ANN001, ANN201
        return ""

    def evaluate_tx_cbor(self, cbor):  # noqa: ANN001, ANN201
        return {}


def _offline_ctx() -> _OfflineContext:
    """A fresh network-free chain context for builder assembly."""
    return _OfflineContext()


def _actor_addr() -> Address:
    """A deterministic placeholder mainnet actor address (key payment + stake)."""
    return Address(
        payment_part=VerificationKeyHash(bytes.fromhex("11" * 28)),
        staking_part=VerificationKeyHash(bytes.fromhex("22" * 28)),
        network=Network.MAINNET,
    )


def _script_hash(u: Utxo) -> str:
    """Script hash (hex) of a UTxO's attached PlutusV3 reference script."""
    return str(plutus_script_hash(PlutusV3Script(bytes.fromhex(u.ref_script or ""))))


def _parses(datum: str | None, cls: type[PlutusData]) -> bool:
    """True if `datum` (CBOR hex) decodes as the given `PlutusData` subclass."""
    if not datum:
        return False
    try:
        cls.from_cbor(datum)
        return True
    except Exception:  # noqa: BLE001 - any decode failure means "not this datum"
        return False


@pytest.fixture
def offline_ctx() -> _OfflineContext:
    """A fresh network-free chain context for builder assembly."""
    return _offline_ctx()


@pytest.fixture
def actor_addr() -> Address:
    """A deterministic placeholder mainnet actor address."""
    return _actor_addr()


@pytest.fixture
def script_hash() -> Callable[[Utxo], str]:
    """The reference-script hash helper (returned as a callable)."""
    return _script_hash


@pytest.fixture
def parses() -> Callable[[str | None, type[PlutusData]], bool]:
    """The "does this datum decode as `cls`?" probe (returned as a callable)."""
    return _parses


def _reconstruct_loan_snapshot_kwargs(
    fix: dict,
    *,
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type[PlutusData]], bool],
) -> dict:
    """Classify a captured loan-spend tx back into snapshot constructor kwargs.

    The fixture is a post-state tx, so the snapshot's read-side UTxOs are classified
    back out of it: the protocol-config (config NFT) yields the derived script hashes,
    the market-param + pool UTxOs pair by the pool NFT (`config_pool_skh` +
    `market_name`), the loan UTxO is the spend input carrying the market loan token
    (with a `LoanDatum`), the three reference scripts match by script hash, and the
    remaining oracle reference UTxOs split into the Danogo-owned config / path datums
    and the external price-source leaves. The resulting kwargs are shared by the repay
    (`RepaySnapshot`) and increase-loan (`IncreaseLoanSnapshot`) reconstructions, which
    resolve the identical read side.
    """
    ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
    inputs = [_as_utxo(u) for u in fix["inputs"]]

    loan_skh = fix["loan_skh"]
    market_name = fix["market_name"]
    config_pool_skh = fix["config_pool_skh"]

    cfg_nft = PROTOCOL_CONFIG_NFT or ""
    cfg_policy, cfg_name = cfg_nft[:56], cfg_nft[56:]
    protocol_config = next(u for u in ref_inputs if u.holds(cfg_policy, cfg_name))
    pd = ProtocolDatum.from_cbor(protocol_config.datum)

    market = next(u for u in ref_inputs if u.holds(config_pool_skh, market_name))
    market_info = DanogoMarket.from_market_datum(market.datum)
    pool = next(
        u
        for u in inputs
        if u.holds(config_pool_skh, market_name) and parses(u.datum, PoolDatum)
    )
    loan = next(
        u
        for u in inputs
        if u.holds(loan_skh, market_name, 1) and parses(u.datum, LoanDatum)
    )
    loan_datum = LoanDatum.from_cbor(bytes.fromhex(loan.datum))

    by_hash = {script_hash(u): u for u in ref_inputs if u.ref_script}
    # The Danogo-owned oracle config / path UTxOs are the reference inputs carrying an
    # asset under the deployment's oracle-config mint policy (the global-config NFT and
    # the path-config FTs/NFTs share it). Classifying by the policy -- not by which
    # datum class happens to parse -- buckets both the packed deployment's configs and
    # the structured deployment's `Constr0` configs (which parse as neither packed
    # class), leaving the rest as external price-source leaves.
    deployment = deployment_for_oracle_skh(pd.oracle_skh.hex())
    oracle_data_refs = [
        u
        for u in ref_inputs
        if any(p == deployment.path_config_policy for p, _n, _q in u.assets)
    ]
    classified = {id(protocol_config), id(market)}
    classified |= {id(u) for u in by_hash.values()}
    classified |= {id(u) for u in oracle_data_refs}
    oracle_source_leaves = [u for u in ref_inputs if id(u) not in classified]

    return {
        "market_name": market_name,
        "loan_skh": loan_skh,
        "pool_skh": pd.pool_skh.hex(),
        "config_pool_skh": config_pool_skh,
        "oracle_skh": pd.oracle_skh.hex(),
        "protocol_config": protocol_config,
        "market": market,
        "market_info": market_info,
        "pool": pool,
        "loan": loan,
        "loan_datum": loan_datum,
        "owner_nft": loan_datum.owner_nft.unit(),
        "oracle_data_refs": oracle_data_refs,
        "pool_script_ref": by_hash[POOL_SCRIPT_SKH],
        "loan_mint_script_ref": by_hash[LOAN_MINT_SCRIPT_SKH],
        # The oracle reference-script hash is derived from the protocol config
        # (`oracle_skh`), not a fixed constant: across deployments it rotates, so the
        # captured fixtures span more than one oracle script hash.
        "oracle_script_ref": by_hash[pd.oracle_skh.hex()],
        "oracle_source_leaves": oracle_source_leaves,
    }


@pytest.fixture
def repay_snap(
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type[PlutusData]], bool],
) -> Callable[[str], tuple[dict, RepaySnapshot]]:
    """Factory rebuilding a `RepaySnapshot` from a captured decrease-loan tx.

    Shared by the offline structural build test and the live (Ogmios) evaluate test,
    which both reconstruct the same snapshot from the captured post-state tx (see
    `_reconstruct_loan_snapshot_kwargs`).
    """

    def _make(name: str) -> tuple[dict, RepaySnapshot]:
        fix = json.loads((FIXTURES_DIR / name).read_text())
        kwargs = _reconstruct_loan_snapshot_kwargs(
            fix,
            script_hash=script_hash,
            parses=parses,
        )
        return fix, RepaySnapshot(**kwargs)

    return _make


@pytest.fixture
def increase_snap(
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type[PlutusData]], bool],
) -> Callable[[str], tuple[dict, IncreaseLoanSnapshot]]:
    """Factory rebuilding an `IncreaseLoanSnapshot` from a captured increase-loan tx.

    Increase-loan resolves the identical read side as repay (it spends the pool + an
    open loan and prices the loan's collateral), so the snapshot is reconstructed from
    the captured post-state tx via the same classifier
    (`_reconstruct_loan_snapshot_kwargs`). Shared by the offline structural build test
    and the live (Ogmios) evaluate test.
    """

    def _make(name: str) -> tuple[dict, IncreaseLoanSnapshot]:
        fix = json.loads((FIXTURES_DIR / name).read_text())
        kwargs = _reconstruct_loan_snapshot_kwargs(
            fix,
            script_hash=script_hash,
            parses=parses,
        )
        return fix, IncreaseLoanSnapshot(**kwargs)

    return _make


def _reconstruct_modify_snapshot_kwargs(
    fix: dict,
    *,
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type[PlutusData]], bool],
) -> dict:
    """Classify a captured modify-collateral tx back into snapshot constructor kwargs.

    A modify spends ONLY the loan (the pool is a REFERENCE input, never spent), so the
    classifier differs from `_reconstruct_loan_snapshot_kwargs` in two ways: the pool
    is found among the REFERENCE inputs (the one carrying the pool NFT that parses as a
    `PoolDatum`) rather than the spent inputs, and there is NO pool reference script
    (the pool validator never runs) -- only the loan-mint + oracle reference scripts.
    The loan UTxO is the spend input carrying the market loan token; the remaining
    oracle reference UTxOs split into the Danogo-owned config / path datums and the
    external price-source leaves (a second pool referenced purely for cross-quote
    pricing stays a leaf -- it does not carry the loan market's pool NFT).
    """
    ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
    inputs = [_as_utxo(u) for u in fix["inputs"]]

    loan_skh = fix["loan_skh"]
    market_name = fix["market_name"]
    config_pool_skh = fix["config_pool_skh"]

    cfg_nft = PROTOCOL_CONFIG_NFT or ""
    cfg_policy, cfg_name = cfg_nft[:56], cfg_nft[56:]
    protocol_config = next(u for u in ref_inputs if u.holds(cfg_policy, cfg_name))
    pd = ProtocolDatum.from_cbor(protocol_config.datum)

    # Both the market-param and pool UTxOs carry the pool NFT (config_pool_skh +
    # market_name); the pool parses as a `PoolDatum`, the market-param does not.
    pool = next(
        u
        for u in ref_inputs
        if u.holds(config_pool_skh, market_name) and parses(u.datum, PoolDatum)
    )
    market = next(
        u
        for u in ref_inputs
        if u.holds(config_pool_skh, market_name) and not parses(u.datum, PoolDatum)
    )
    market_info = DanogoMarket.from_market_datum(market.datum)
    loan = next(
        u
        for u in inputs
        if u.holds(loan_skh, market_name, 1) and parses(u.datum, LoanDatum)
    )
    loan_datum = LoanDatum.from_cbor(bytes.fromhex(loan.datum))

    by_hash = {script_hash(u): u for u in ref_inputs if u.ref_script}
    deployment = deployment_for_oracle_skh(pd.oracle_skh.hex())
    oracle_data_refs = [
        u
        for u in ref_inputs
        if any(p == deployment.path_config_policy for p, _n, _q in u.assets)
    ]
    classified = {id(protocol_config), id(market), id(pool)}
    classified |= {id(u) for u in by_hash.values()}
    classified |= {id(u) for u in oracle_data_refs}
    oracle_source_leaves = [u for u in ref_inputs if id(u) not in classified]

    return {
        "market_name": market_name,
        "loan_skh": loan_skh,
        "pool_skh": pd.pool_skh.hex(),
        "config_pool_skh": config_pool_skh,
        "oracle_skh": pd.oracle_skh.hex(),
        "protocol_config": protocol_config,
        "market": market,
        "market_info": market_info,
        "pool": pool,
        "loan": loan,
        "loan_datum": loan_datum,
        "owner_nft": loan_datum.owner_nft.unit(),
        "oracle_data_refs": oracle_data_refs,
        "loan_mint_script_ref": by_hash[LOAN_MINT_SCRIPT_SKH],
        "oracle_script_ref": by_hash[pd.oracle_skh.hex()],
        "oracle_source_leaves": oracle_source_leaves,
    }


@pytest.fixture
def modify_snap(
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type[PlutusData]], bool],
) -> Callable[[str], tuple[dict, ModifyCollateralSnapshot]]:
    """Factory rebuilding a `ModifyCollateralSnapshot` from a captured modify tx.

    A modify spends only the loan and references the pool, so it reconstructs the
    snapshot via the modify-specific classifier (`_reconstruct_modify_snapshot_kwargs`,
    which finds the pool among the reference inputs and resolves no pool script).
    Shared by the offline structural build test and the live (Ogmios) evaluate test.
    """

    def _make(name: str) -> tuple[dict, ModifyCollateralSnapshot]:
        fix = json.loads((FIXTURES_DIR / name).read_text())
        kwargs = _reconstruct_modify_snapshot_kwargs(
            fix,
            script_hash=script_hash,
            parses=parses,
        )
        return fix, ModifyCollateralSnapshot(**kwargs)

    return _make


def _reconstruct_topup_snapshot_kwargs(
    fix: dict,
    *,
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type[PlutusData]], bool],
) -> dict:
    """Classify a captured deposit/withdraw (TopupWithdraw) tx into snapshot kwargs.

    A deposit/withdraw SPENDS the pool (like create-loan, unlike modify) but no loan:
    the pool is the spent input carrying the pool NFT (`config_pool_skh` +
    `market_name`) that parses as a `PoolDatum`, the market-param is the reference
    input carrying the same pool NFT that does not, and the protocol-config (config
    NFT) yields the derived script hashes. The reference scripts match by script hash
    and the remaining oracle reference UTxOs split into the Danogo-owned config / path
    datums and the external price-source leaves -- the same split the loan snapshot
    uses. An alt-supply market additionally references the oracle script + leaves; a
    single-supply-token market would carry none, but the captured fixture is an
    alt-supply pool, so they are always present here.
    """
    ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
    inputs = [_as_utxo(u) for u in fix["inputs"]]

    market_name = fix["market_name"]
    config_pool_skh = fix["config_pool_skh"]

    cfg_nft = PROTOCOL_CONFIG_NFT or ""
    cfg_policy, cfg_name = cfg_nft[:56], cfg_nft[56:]
    protocol_config = next(u for u in ref_inputs if u.holds(cfg_policy, cfg_name))
    pd = ProtocolDatum.from_cbor(protocol_config.datum)

    pool = next(
        u
        for u in inputs
        if u.holds(config_pool_skh, market_name) and parses(u.datum, PoolDatum)
    )
    market = next(
        u
        for u in ref_inputs
        if u.holds(config_pool_skh, market_name) and not parses(u.datum, PoolDatum)
    )
    market_info = DanogoMarket.from_market_datum(market.datum)

    by_hash = {script_hash(u): u for u in ref_inputs if u.ref_script}
    deployment = deployment_for_oracle_skh(pd.oracle_skh.hex())
    oracle_data_refs = [
        u
        for u in ref_inputs
        if any(p == deployment.path_config_policy for p, _n, _q in u.assets)
    ]
    classified = {id(protocol_config), id(market)}
    classified |= {id(u) for u in by_hash.values()}
    classified |= {id(u) for u in oracle_data_refs}
    oracle_source_leaves = [u for u in ref_inputs if id(u) not in classified]

    return {
        "market_name": market_name,
        "pool_skh": pd.pool_skh.hex(),
        "config_pool_skh": config_pool_skh,
        "oracle_skh": pd.oracle_skh.hex(),
        "protocol_config": protocol_config,
        "market": market,
        "market_info": market_info,
        "pool": pool,
        "pool_script_ref": by_hash[POOL_SCRIPT_SKH],
        "oracle_script_ref": by_hash[pd.oracle_skh.hex()],
        "oracle_data_refs": oracle_data_refs,
        "oracle_source_leaves": oracle_source_leaves,
    }


@pytest.fixture
def topup_snap(
    script_hash: Callable[[Utxo], str],
    parses: Callable[[str | None, type[PlutusData]], bool],
) -> Callable[[str], tuple[dict, TopupWithdrawSnapshot]]:
    """Factory rebuilding a `TopupWithdrawSnapshot` from a captured deposit/withdraw tx.

    A deposit/withdraw spends the pool (no loan) and, for an alt-supply market,
    re-prices the alternative supply tokens through the oracle. The snapshot is
    reconstructed from the captured post-state tx via
    `_reconstruct_topup_snapshot_kwargs`. Shared by the offline byte-exact datum test
    and the live (Ogmios) deposit evaluate test.
    """

    def _make(name: str) -> tuple[dict, TopupWithdrawSnapshot]:
        fix = json.loads((FIXTURES_DIR / name).read_text())
        kwargs = _reconstruct_topup_snapshot_kwargs(
            fix,
            script_hash=script_hash,
            parses=parses,
        )
        return fix, TopupWithdrawSnapshot(**kwargs)

    return _make
