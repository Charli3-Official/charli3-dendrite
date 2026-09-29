"""SundaeSwap V4 stableswap datums against recorded mainnet and testnet CBOR.

Module redeemers (Create / Operate) round-trip byte-exact, configs hash to the
creation state's ``module_state`` commitment, and transcript steps parse to their
recorded values. A step's attribution slot is opaque (the served order's output
reference, or Void); its recorded definite-length list is read, not re-encoded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pycardano import DeserializeException
from pycardano import IndefiniteList
from pycardano import RawPlutusData

from charli3_dendrite.dexs.amm.sundae_v4 import BoolFalse
from charli3_dendrite.dexs.amm.sundae_v4 import BoolTrue
from charli3_dendrite.dexs.amm.sundae_v4 import MultisigSignature
from charli3_dendrite.dexs.amm.sundae_v4 import OptionNone
from charli3_dendrite.dexs.amm.sundae_v4 import OptionSomeMultisig
from charli3_dendrite.dexs.amm.sundae_v4 import OptionSomeRational
from charli3_dendrite.dexs.amm.sundae_v4 import OutputReference
from charli3_dendrite.dexs.amm.sundae_v4 import PoolAction
from charli3_dendrite.dexs.amm.sundae_v4 import Rational
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapConfig
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapCreate
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapLiquidityStep
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapLiquidityStepV0
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapOperate
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapRateUpdate
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapSwapStep
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapSwapStepV0
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import INVARIANT_MODULE_KINDS
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.amm.sundae_v4 import parse_stableswap_step

_HERE = Path(__file__).parent
MAINNET = json.loads((_HERE / "sundae_v4_stableswap_mainnet_fixtures.json").read_text())
TESTNET = json.loads((_HERE / "sundae_v4_stableswap_testnet_fixtures.json").read_text())
_NETS = TESTNET["networks"]
SS_MODULES = {
    MAINNET["module"],
    _NETS["preview"]["stableswap_module_hash_current"],
    _NETS["preview"]["stableswap_module_hash_superseded1"],
    _NETS["preprod"]["stableswap_module_hash_current"],
}
POOL_SPENDS = {
    SundaeV4Deployment.for_network("mainnet").pool_hash.hex(),
    _NETS["preview"]["pool_spend_hash"],
    _NETS["preprod"]["pool_spend_hash"],
}
_CREATE_TAG, _OPERATE_TAG = 121, 122


def all_records() -> list[dict]:
    """Every recorded redeemer, mainnet then testnet."""
    out = [r for rs in MAINNET["redeemers"].values() for r in rs]
    out += [r for t in TESTNET["transactions"] for r in t["redeemers"]]
    return out


def module_redeemers(tag: int) -> list[dict]:
    """The recorded stableswap-module redeemers with CBOR constructor ``tag``."""
    return [
        r
        for r in all_records()
        if r["script_hash"] in SS_MODULES
        and RawPlutusData.from_cbor(bytes.fromhex(r["data_cbor"])).data.tag == tag
    ]


def commitment(datum_hex: str, module: str) -> bytes:
    """The ``module_state`` slot a vault datum holds for ``module``."""
    datum = SundaeV4PoolDatum.from_cbor(datum_hex)
    return {bytes(k).hex(): bytes(v) for k, v in datum.module_state}[module]


def vault_steps(tx: str, output_index: int) -> list:
    """The transcript of the pool spend in ``tx`` that produced output ``output_index``."""
    for r in MAINNET["redeemers"][tx]:
        if r["purpose"] != "spend" or r["script_hash"] not in POOL_SPENDS:
            continue
        action = PoolAction.from_cbor(r["data_cbor"])
        if action.pool_output_index == output_index:
            return list(action.transcript)
    return []


def test_module_redeemers_round_trip_byte_exact() -> None:
    creates = module_redeemers(_CREATE_TAG)
    operates = module_redeemers(_OPERATE_TAG)
    assert len(creates) >= 5 and len(operates) >= 15
    for r in creates:
        assert (
            StableSwapCreate.from_cbor(r["data_cbor"]).to_cbor_hex() == r["data_cbor"]
        )
    for r in operates:
        assert (
            StableSwapOperate.from_cbor(r["data_cbor"]).to_cbor_hex() == r["data_cbor"]
        )


def test_create_configs_hash_to_the_creation_commitment() -> None:
    first = MAINNET["pool_states"][0]
    (create,) = [
        r
        for r in MAINNET["redeemers"][first["tx"]]
        if r["script_hash"] == MAINNET["module"]
    ]
    config = StableSwapCreate.from_cbor(create["data_cbor"]).initial_state
    assert module_config_hash(config) == commitment(first["datum"], MAINNET["module"])
    for t in TESTNET["transactions"]:
        for r in t["redeemers"]:
            raw = RawPlutusData.from_cbor(bytes.fromhex(r["data_cbor"])).data
            if r["script_hash"] not in SS_MODULES or raw.tag != _CREATE_TAG:
                continue
            config = StableSwapCreate.from_cbor(r["data_cbor"]).initial_state
            assert any(
                module_config_hash(config) == commitment(s["datum"], r["script_hash"])
                for s in t["pool_states"]
                if r["script_hash"]
                in {
                    bytes(k).hex()
                    for k, _ in SundaeV4PoolDatum.from_cbor(s["datum"]).module_state
                }
            )


def test_config_fields_decode_both_option_and_bool_shapes() -> None:
    first = MAINNET["pool_states"][0]
    (create,) = [
        r
        for r in MAINNET["redeemers"][first["tx"]]
        if r["script_hash"] == MAINNET["module"]
    ]
    mainnet = StableSwapCreate.from_cbor(create["data_cbor"]).initial_state
    assert mainnet.linear_amplification == 500
    assert (mainnet.fee.num, mainnet.fee.den) == (15, 10_000)
    assert list(mainnet.rates) == [1_000_000, 1_000_000]
    assert isinstance(mainnet.rate_manager, OptionSomeMultisig)
    assert isinstance(mainnet.monotone_rates, BoolFalse)
    assert isinstance(mainnet.max_rate_step, OptionNone)
    preview = [
        StableSwapCreate.from_cbor(r["data_cbor"]).initial_state
        for r in module_redeemers(_CREATE_TAG)
        if r["script_hash"] == _NETS["preview"]["stableswap_module_hash_current"]
    ]
    assert preview
    assert all(isinstance(c.monotone_rates, BoolTrue) for c in preview)
    assert all(isinstance(c.max_rate_step, OptionSomeRational) for c in preview)
    assert all(
        (c.max_rate_step.value.num, c.max_rate_step.value.den) == (1, 100)
        for c in preview
    )


def test_a_built_config_hashes_like_the_recorded_one() -> None:
    first = MAINNET["pool_states"][0]
    (create,) = [
        r
        for r in MAINNET["redeemers"][first["tx"]]
        if r["script_hash"] == MAINNET["module"]
    ]
    recorded = StableSwapCreate.from_cbor(create["data_cbor"]).initial_state
    built = StableSwapConfig(
        linear_amplification=500,
        fee=Rational(num=15, den=10_000),
        rates=IndefiniteList([1_000_000, 1_000_000]),
        rate_manager=OptionSomeMultisig(
            value=MultisigSignature(key_hash=recorded.rate_manager.value.key_hash)
        ),
        monotone_rates=BoolFalse(),
        max_rate_step=OptionNone(),
    )
    assert module_config_hash(built) == module_config_hash(recorded)


def test_mainnet_transcript_steps_parse_to_their_recorded_values() -> None:
    kinds: dict[int, int] = {}
    rates_seen: list[list[int]] = []
    for state in MAINNET["pool_states"][1:]:
        for entry in vault_steps(state["tx"], state["index"]):
            step = parse_stableswap_step(entry.operation_tag, entry.operation_data)
            kinds[entry.operation_tag] = kinds.get(entry.operation_tag, 0) + 1
            if entry.operation_tag == 3:
                assert isinstance(step, StableSwapSwapStep)
                assert step.raw_swap_result > 0 and step.next_sum_invariant > 0
                ref = OutputReference.from_cbor(step.attribution.to_cbor())
                assert len(ref.transaction_id) == 32
            elif entry.operation_tag in (4, 6):
                assert isinstance(step, StableSwapLiquidityStep)
                assert (step.target_delta_d > 0) == (entry.operation_tag == 6)
            else:
                assert isinstance(step, StableSwapRateUpdate)
                rates_seen.append([int(r) for r in step.rates])
    assert kinds == {3: 4, 7: 2, 6: 4, 4: 1}
    assert rates_seen == [[1_000_000, 1_000_001], [1_000_000, 1_000_000]]


def test_superseded_shapes_are_two_field_steps() -> None:
    v0 = StableSwapSwapStepV0(raw_swap_result=7, next_sum_invariant=11)
    assert parse_stableswap_step(3, v0.to_cbor(), superseded=True) == v0
    liq = StableSwapLiquidityStepV0(target_delta_d=-3, next_sum_invariant=5)
    assert parse_stableswap_step(4, liq.to_cbor(), superseded=True) == liq
    with pytest.raises(DeserializeException):
        parse_stableswap_step(3, v0.to_cbor())
    current = StableSwapSwapStep(
        raw_swap_result=7,
        next_sum_invariant=11,
        attribution=RawPlutusData(OptionNone().to_primitive()),
    )
    with pytest.raises(DeserializeException):
        parse_stableswap_step(3, current.to_cbor(), superseded=True)


@pytest.mark.parametrize("tag", [0, 1, 2, 5, 8])
def test_a_tag_the_module_has_no_step_for_raises(tag: int) -> None:
    with pytest.raises(ValueError, match="not a stableswap step"):
        parse_stableswap_step(tag, b"\xd8\x79\x80")


@pytest.mark.parametrize(
    ("network", "title", "applied"),
    [
        (
            "mainnet",
            "stableswap.withdraw",
            "f47f6594cab956302f7f1cd81ac4122e9ee128146102497feca1a79f",
        ),
        (
            "preview",
            "stableswap.withdraw",
            "9db7ce54fb25f4390a89bb715a022fe79a86b9a043aa22c31c27380a",
        ),
        (
            "preview",
            "stableswap.withdraw.superseded1",
            "a44e0058459a223752be78dc4df7ca86446ba22ef95c7ee690e0b5a3",
        ),
        (
            "preprod",
            "stableswap.withdraw",
            "031b29852b494e33ab3b66a4df53d28dd8d00af15b59d3c5a6529db0",
        ),
    ],
)
def test_every_stableswap_build_is_the_stableswap_module(
    network, title, applied
) -> None:
    deployment = SundaeV4Deployment.for_network(network)
    assert deployment.validator(title).hex() == applied
    assert deployment.module_kind(bytes.fromhex(applied)) == "stableswap"
    assert deployment.module_kind(deployment.constant_sum_hash) == "constant_sum"
    assert deployment.module_kind(deployment.pool_hash) is None


def test_manifest_carries_the_stableswap_references_and_pool_configs() -> None:
    mainnet = SundaeV4Deployment.for_network("mainnet")
    assert mainnet.stableswap_hash.hex() == MAINNET["module"]
    assert mainnet.reference("stableswap.withdraw") == (
        "419cbf3bc96169d0a46cc9d5a122e8a9ee1f4b1f71625911def64294468dfc8d",
        0,
    )
    assert mainnet.config_token("ss-pool").hex() == (
        "00c250095fc26d302cd932fd84f89b1b66fc8f6d9a21b1d59f2d7c839f1c23cc"
    )
    preview = SundaeV4Deployment.for_network("preview")
    assert preview.config_token("ss-old-pool").hex() == (
        "00c334ba9af39b245b3dd97e43e46920c8f8a1ad42bddfe936f546bb349f71e9"
    )
    preprod = SundaeV4Deployment.for_network("preprod")
    assert preprod.reference("stableswap.withdraw") == (
        "5d8891affb8e84f755e683317415408fc06a72be4c8b455ebc1b0c9f5e6aad0f",
        0,
    )


def test_the_mainnet_vault_binds_one_stableswap_module() -> None:
    SundaeV4Vault.select_network("mainnet")
    state = MAINNET["pool_states"][-1]
    vault = SundaeV4Vault.model_validate(
        {
            "tx_hash": state["tx"],
            "tx_index": state["index"],
            "datum_cbor": state["datum"],
            "assets": state["value"],
            "block_time": state["block_time"],
        }
    )
    assert "stableswap" in INVARIANT_MODULE_KINDS
    assert vault.invariant_modules() == [
        (100, bytes.fromhex(MAINNET["module"]), "stableswap")
    ]
