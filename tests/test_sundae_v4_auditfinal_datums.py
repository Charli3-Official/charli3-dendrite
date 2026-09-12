"""Byte-exact parsing tests against the audit-final SundaeSwap V4 deployments.

``sundae_v4_auditfinal_fixtures.json`` holds verbatim on-chain CBOR from the
2026-09-07 preview and preprod deployments: settings nodes, pool and order datums,
and the redeemers of real scoops. Every datum / redeemer class must decode the
live bytes and re-encode them byte-for-byte, and the two derivation rules the
contracts pin (pool identifier from the seed output reference, module_state
commitment from the module config) must reproduce the on-chain values.
"""

import hashlib
import json
from pathlib import Path

import cbor2
import pytest
from pycardano import RawPlutusData

from charli3_dendrite.dataclasses.datums import AssetClass

from charli3_dendrite.dexs.amm.sundae_v4 import BasicClaim
from charli3_dendrite.dexs.amm.sundae_v4 import BasicConstraintKind
from charli3_dendrite.dexs.amm.sundae_v4 import BasicDeposit
from charli3_dendrite.dexs.amm.sundae_v4 import BasicSwap
from charli3_dendrite.dexs.amm.sundae_v4 import BasicWithdraw
from charli3_dendrite.dexs.amm.sundae_v4 import BountyClaim
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumConfig
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumCreate
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumOperate
from charli3_dendrite.dexs.amm.sundae_v4 import Destroy
from charli3_dendrite.dexs.amm.sundae_v4 import FairnessOperate
from charli3_dendrite.dexs.amm.sundae_v4 import FeeSettings
from charli3_dendrite.dexs.amm.sundae_v4 import FeeSplitOperate
from charli3_dendrite.dexs.amm.sundae_v4 import OrderConfig
from charli3_dendrite.dexs.amm.sundae_v4 import OrderScoop
from charli3_dendrite.dexs.amm.sundae_v4 import OrderValidatorRedeemer
from charli3_dendrite.dexs.amm.sundae_v4 import PoolAction
from charli3_dendrite.dexs.amm.sundae_v4 import PoolConfig
from charli3_dendrite.dexs.amm.sundae_v4 import SettingsDatum
from charli3_dendrite.dexs.amm.sundae_v4 import StrategyExecution
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SwapConstraint
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.amm.sundae_v4 import parse_basic_constraint
from charli3_dendrite.dexs.amm.sundae_v4 import pool_identifier

FIXTURES = json.loads(
    (Path(__file__).parent / "sundae_v4_auditfinal_fixtures.json").read_text(),
)
CS_HASH = {
    "preview": "fa7f4860b25488fd63f2a7f5be4e8711520d288074b01286c9f14344",
    "preprod": "b22b5293a1d6265a5a02564436bf35f6f3cef85654f565c2759cce4b",
}


def _roundtrip(cls, hex_cbor: str):
    """Decode ``hex_cbor`` and require the re-encoding to be byte-identical."""
    obj = cls.from_cbor(bytes.fromhex(hex_cbor))
    assert obj.to_cbor_hex() == hex_cbor, f"{cls.__name__} does not round trip"
    return obj


def _lossless(cls, hex_cbor: str):
    """Decode ``hex_cbor`` and require a lossless (structurally equal) re-encoding.

    Used for redeemers built by third parties: their opaque ``Data`` payloads may
    use definite-length arrays, which pycardano re-emits indefinite. Plutus data
    equality ignores that framing, so the decoded structure is what must match.
    """
    obj = cls.from_cbor(bytes.fromhex(hex_cbor))
    assert cbor2.loads(obj.to_cbor()) == cbor2.loads(bytes.fromhex(hex_cbor))
    assert cls.from_cbor(obj.to_cbor()) == obj
    return obj


# ---------------------------------------------------------------------------
# Pool datum
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pool", FIXTURES["pool_datums"], ids=lambda p: p["label"])
def test_pool_datum_round_trips_nine_fields(pool):
    datum = _roundtrip(SundaeV4PoolDatum, pool["datum"])
    assert datum.identifier.hex() == pool["identifier"]
    assert datum.min_surplus == 5_000_000
    assert isinstance(datum.extension, RawPlutusData)


def test_pool_datum_lovelace_reserve_is_the_empty_asset_class():
    ada_pool = next(p for p in FIXTURES["pool_datums"] if "lovelace" in p["label"])
    datum = SundaeV4PoolDatum.from_cbor(bytes.fromhex(ada_pool["datum"]))
    first_asset = AssetClass.from_primitive(datum.assets[0][0])
    assert first_asset.policy == b"" and first_asset.asset_name == b""


@pytest.mark.parametrize(
    "pool",
    [p for p in FIXTURES["pool_datums"] if "seed_tx_hash" in p],
    ids=lambda p: p["label"],
)
def test_pool_identifier_derives_from_the_seed_output_reference(pool):
    assert pool_identifier(
        bytes.fromhex(pool["seed_tx_hash"]), pool["seed_index"]
    ) == bytes.fromhex(pool["identifier"])


# ---------------------------------------------------------------------------
# Module configs and their module_state commitment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pool",
    [p for p in FIXTURES["pool_datums"] if "module_create_redeemers" in p],
    ids=lambda p: p["label"],
)
def test_constant_sum_create_config_round_trips_and_commits_to_module_state(pool):
    create = _roundtrip(
        ConstantSumCreate, pool["module_create_redeemers"]["constant_sum"]
    )
    config = create.initial_state
    assert isinstance(config, ConstantSumConfig)
    assert config.balance_fee.den > 0
    assert (
        module_config_hash(config).hex()
        == pool["module_state"][CS_HASH[pool["network"]]]
    )


def test_constant_sum_config_carries_four_fields():
    pool = FIXTURES["pool_datums"][
        2
    ]  # USDr/USDCx: fee 1/1000, bounty off, balance_fee 0
    config = ConstantSumCreate.from_cbor(
        bytes.fromhex(pool["module_create_redeemers"]["constant_sum"]),
    ).initial_state
    assert list(config.prices) == [1, 1]
    assert (config.fee.num, config.fee.den) == (1, 1000)
    assert (config.bounty_k.num, config.bounty_k.den) == (1, 2000)
    assert (config.balance_fee.num, config.balance_fee.den) == (0, 1)


# ---------------------------------------------------------------------------
# Settings nodes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("network", ["preview", "preprod"])
def test_settings_datum_round_trips(network):
    settings = _roundtrip(SettingsDatum, FIXTURES["settings_datum"][network])
    assert len(settings.authorized_scoopers.scoopers) == 2


@pytest.mark.parametrize("role", ["basic", "strategy"])
def test_order_config_lists_trade_then_fee_constraint(role):
    config = _roundtrip(OrderConfig, FIXTURES["order_config_datums"][role])
    assert len(config.required_constraints) == 2
    assert (
        config.required_constraints[1].hex()
        == "d7d1c09deb8bc8e15baae7895e57e86ab6d0cb10e2faaa6d96cd691b"
    )


def test_pool_config_round_trips():
    config = _roundtrip(PoolConfig, FIXTURES["pool_config_datum"])
    assert [a.tag for a in config.actions] == [100, 200, 1]
    assert config.min_surplus == 5_000_000


def test_fee_settings_round_trips():
    assert _roundtrip(FeeSettings, FIXTURES["fee_settings_datum"]).base_fee == 1_000_000


# ---------------------------------------------------------------------------
# Order datums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", FIXTURES["order_datums"], ids=lambda o: o["label"])
def test_order_datum_round_trips_with_fee_fields(order):
    datum = _roundtrip(SundaeV4OrderDatum, order["datum"])
    assert datum.service_budget == 3_000_000
    assert datum.max_per_execution in (1_000_000, 3_000_000)
    assert len(datum.constraints) == 2


def test_basic_constraint_kind_is_the_payload_constructor():
    swap_order = SundaeV4OrderDatum.from_cbor(
        bytes.fromhex(FIXTURES["order_datums"][0]["datum"])
    )
    deposit_order = SundaeV4OrderDatum.from_cbor(
        bytes.fromhex(FIXTURES["order_datums"][2]["datum"])
    )
    swap = parse_basic_constraint(swap_order.constraints[0][1])
    deposit = parse_basic_constraint(deposit_order.constraints[0][1])
    assert isinstance(swap, BasicSwap) and swap.kind is BasicConstraintKind.SWAP
    assert (
        isinstance(deposit, BasicDeposit)
        and deposit.kind is BasicConstraintKind.DEPOSIT
    )
    assert len(swap.offered) == 1 and len(deposit.offered) == 2


def test_basic_constraint_variants_share_one_field_shape():
    payload = SundaeV4OrderDatum.from_cbor(
        bytes.fromhex(FIXTURES["order_datums"][0]["datum"])
    ).constraints[0][1]
    hex_cbor = RawPlutusData(payload).to_cbor_hex()
    swap = BasicSwap.from_cbor(bytes.fromhex(hex_cbor))
    assert swap.to_cbor_hex() == hex_cbor
    withdraw = BasicWithdraw(offered=swap.offered, min_received=swap.min_received)
    claim = BasicClaim(offered=swap.offered, min_received=swap.min_received)
    assert withdraw.kind is BasicConstraintKind.WITHDRAW
    assert claim.kind is BasicConstraintKind.CLAIM


def test_strategy_execution_names_signed_min_deltas():
    assert "min_deltas" in StrategyExecution.__dataclass_fields__
    assert "min_received" not in StrategyExecution.__dataclass_fields__


# ---------------------------------------------------------------------------
# Scoop redeemers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scoop", FIXTURES["scoops"], ids=lambda s: s["label"])
def test_pool_action_redeemer_round_trips(scoop):
    for spend in scoop["pool_spends"]:
        action = _lossless(PoolAction, spend["redeemer"])
        assert action.tag == 100
        assert action.pool_input_index == spend["input_index"]


def test_three_step_transcript_threads_state_to_the_output_datum():
    scoop = next(s for s in FIXTURES["scoops"] if "3-step" in s["label"])
    spend = scoop["pool_spends"][0]
    action = PoolAction.from_cbor(bytes.fromhex(spend["redeemer"]))
    assert [e.operation_tag for e in action.transcript] == [3, 3, 3]
    output = SundaeV4PoolDatum.from_cbor(bytes.fromhex(spend["pool_output_datum"]))
    final = action.transcript[-1].state_after
    assert final.assets == output.assets and final.total_lp == output.total_lp


def test_deposit_step_declares_its_value_delta():
    scoop = next(s for s in FIXTURES["scoops"] if "tag-6 deposit USDr" in s["label"])
    action = PoolAction.from_cbor(bytes.fromhex(scoop["pool_spends"][0]["redeemer"]))
    (entry,) = action.transcript
    assert entry.operation_tag == 6 and entry.fee_budget == 0
    assert entry.operation_data.data == 394_481_373


def test_destroy_is_pool_redeemer_constructor_four():
    assert Destroy.CONSTR_ID == 4
    assert Destroy.from_cbor(Destroy().to_cbor()) == Destroy()


@pytest.mark.parametrize("scoop", FIXTURES["scoops"], ids=lambda s: s["label"])
def test_module_operate_redeemers_round_trip(scoop):
    reds = scoop["withdraw_redeemers"]
    cs = _roundtrip(ConstantSumOperate, reds["constant_sum"])
    assert all(isinstance(e.config, ConstantSumConfig) for e in cs.entries)
    _roundtrip(FeeSplitOperate, reds["fee_split"])
    fairness = _roundtrip(FairnessOperate, reds["fairness"])
    assert all(e.scooper_idx in (0, 1) for e in fairness.entries)
    _roundtrip(OrderValidatorRedeemer, reds["order"])
    assert int(reds["fee_constraint"], 16) >= 0  # bare designated-pool index


def test_order_scoop_redeemer_names_its_input():
    scoop = FIXTURES["scoops"][0]
    (order_spend,) = scoop["order_spends"]
    assert (
        OrderScoop.from_cbor(bytes.fromhex(order_spend["redeemer"])).own_input_index
        == order_spend["input_index"]
    )


def test_module_config_hash_is_blake2b_256_of_the_serialised_config():
    pool = FIXTURES["pool_datums"][0]
    create = ConstantSumCreate.from_cbor(
        bytes.fromhex(pool["module_create_redeemers"]["constant_sum"])
    )
    expected = hashlib.blake2b(create.initial_state.to_cbor(), digest_size=32).digest()
    assert module_config_hash(create.initial_state) == expected


# ---------------------------------------------------------------------------
# Encodings the audit left unchanged, pinned by pre-audit bytes
# ---------------------------------------------------------------------------


def test_bounty_claim_step_carries_the_claimed_asset_and_amount():
    legacy = FIXTURES["legacy_june_2026_preview"]
    action = PoolAction.from_cbor(bytes.fromhex(legacy["tag5_pool_action_redeemer"]))
    (entry,) = action.transcript
    assert entry.operation_tag == 5
    claim = BountyClaim.from_cbor(entry.operation_data.to_cbor())
    assert isinstance(claim.asset, AssetClass) and claim.amount == 216_746


def test_swap_role_constraint_payload_decodes():
    legacy = FIXTURES["legacy_june_2026_preview"]
    order = SundaeV4OrderDatum.from_cbor(bytes.fromhex(legacy["swap_role_order_datum"]))
    key = bytes.fromhex(legacy["swap_role_constraint_hash"])
    payload = next(entry[1] for entry in order.constraints if entry[0] == key)
    swap = SwapConstraint.from_primitive(payload)
    assert swap.CONSTR_ID == 2
    assert swap.original_offered == swap.remaining_offered > 0
    assert len(swap.min_received) == 1
