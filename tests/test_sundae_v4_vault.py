"""SundaeV4Vault: the pool UTxO primitive parsed from value + datum."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pycardano import RawPlutusData

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import RedeemerRecord
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumConfig
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumCreate
from charli3_dendrite.dexs.amm.sundae_v4 import FeeSplitConfig
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.amm.sundae_v4 import pool_identifier
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.dexs.core.errors import ModuleConfigUnavailableError
from charli3_dendrite.dexs.core.errors import NotAPoolError
from tests.sundae_v4_vault_factory import build_vault_utxo
from tests.test_sundae_v4_backend_redeemers import _Minimal

_FIX = json.loads(
    (Path(__file__).parent / "sundae_v4_auditfinal_fixtures.json").read_text(),
)
_POOLS = [pytest.param(rec, id=rec["label"]) for rec in _FIX["pool_datums"]]


def _unit(entry: list) -> str:
    ac = AssetClass.from_primitive(entry[0])
    return (ac.policy.hex() + ac.asset_name.hex()) or "lovelace"


@pytest.fixture(autouse=True)
def _preview() -> None:
    SundaeV4Vault.select_network("preview")


def _vault(rec: dict) -> SundaeV4Vault:
    SundaeV4Vault.select_network(rec["network"])
    return SundaeV4Vault.model_validate(
        {
            "tx_hash": rec["tx"],
            "tx_index": rec["index"],
            "datum_cbor": rec["datum"],
            "assets": rec["value"],
        },
    )


@pytest.mark.parametrize("rec", _POOLS)
def test_vault_parses_every_live_pool(rec: dict) -> None:
    vault = _vault(rec)
    datum = SundaeV4PoolDatum.from_cbor(bytes.fromhex(rec["datum"]))
    deployment = SundaeV4Deployment.for_network(rec["network"])
    policy = deployment.pool_nft_policy.hex()

    assert vault.identifier == bytes.fromhex(rec["identifier"])
    if "seed_tx_hash" in rec:
        assert vault.identifier == pool_identifier(
            bytes.fromhex(rec["seed_tx_hash"]),
            rec["seed_index"],
        )
    assert vault.pool_nft.unit() == policy + "000de140" + rec["identifier"]
    assert vault.pool_id == vault.pool_nft.unit()
    assert vault.lp_token.unit() == policy + "0014df10" + rec["identifier"]
    assert vault.lp_token.quantity() == rec["value"][vault.lp_token.unit()]

    declared = {_unit(e): int(e[1]) for e in datum.assets}
    assert dict(vault.reserves.root) == declared
    assert list(vault.reserves.root) == sorted(
        declared,
        key=lambda u: "" if u == "lovelace" else u,
    )
    assert vault.datum_units == [_unit(e) for e in datum.assets]
    assert vault.total_lp == datum.total_lp
    assert vault.min_surplus == datum.min_surplus
    assert vault.surplus == rec["value"]["lovelace"] - declared.get("lovelace", 0)
    assert set(vault.module_state) == {bytes(e[0]) for e in datum.module_state}
    for entry in vault.actions:
        for module in entry.modules:
            assert bytes(module) in vault.module_state


@pytest.mark.parametrize("rec", _POOLS)
def test_live_pools_carry_one_constant_sum_invariant_on_tag_100(rec: dict) -> None:
    vault = _vault(rec)
    cs = SundaeV4Deployment.for_network(rec["network"]).validator(
        "constant_sum.withdraw"
    )
    assert vault.invariant_modules() == [(100, cs, "constant_sum")]
    assert vault.module_kind(cs) == "constant_sum"
    assert cs in vault.modules_for(100)
    assert vault.action(100) is not None
    assert vault.action(7) is None


def test_three_asset_vault_parses_three_reserves() -> None:
    units = ["aa" * 28 + "01", "bb" * 28 + "02", "cc" * 28 + "03"]
    values, _ = build_vault_utxo(
        [(units[0], 1_000), (units[2], 3_000), (units[1], 2_000)],
        prices=[1, 3, 2],
        total_lp=6_000,
    )
    vault = SundaeV4Vault.model_validate(values)
    assert list(vault.reserves.root) == units  # canonical policy+name order
    assert vault.reserves[units[2]] == 3_000
    assert vault.datum_units == [units[0], units[2], units[1]]
    assert vault.surplus == 3_000_000


def test_lovelace_reserve_sorts_first_and_surplus_excludes_it() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 5), ("lovelace", 10_000_000)],
        prices=[4, 5],
        total_lp=100,
        surplus=2_500_000,
    )
    vault = SundaeV4Vault.model_validate(values)
    assert vault.reserves.unit(0) == "lovelace"
    assert vault.reserves["lovelace"] == 10_000_000
    assert vault.surplus == 2_500_000


def test_datumless_utxo_is_not_a_pool() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 1), ("bb" * 28 + "02", 1)], prices=[1, 1], total_lp=2
    )
    values["datum_cbor"] = None
    with pytest.raises(NotAPoolError):
        SundaeV4Vault.model_validate(values)


def test_foreign_datum_is_not_a_pool() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 1), ("bb" * 28 + "02", 1)], prices=[1, 1], total_lp=2
    )
    values["datum_cbor"] = "d87980"
    with pytest.raises(NotAPoolError):
        SundaeV4Vault.model_validate(values)


def test_truncated_datum_is_not_a_pool() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 1), ("bb" * 28 + "02", 1)], prices=[1, 1], total_lp=2
    )
    values["datum_cbor"] = "a1"
    with pytest.raises(NotAPoolError):
        SundaeV4Vault.model_validate(values)


def test_missing_pool_nft_is_not_a_pool() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 1), ("bb" * 28 + "02", 1)], prices=[1, 1], total_lp=2
    )
    nft = next(u for u in values["assets"] if "000de140" in u)
    del values["assets"][nft]
    with pytest.raises(NotAPoolError):
        SundaeV4Vault.model_validate(values)


def test_short_reserve_is_invalid() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)], prices=[1, 1], total_lp=200
    )
    values["assets"]["aa" * 28 + "01"] = 99
    with pytest.raises(InvalidPoolError):
        SundaeV4Vault.model_validate(values)


def test_missing_reserve_class_is_invalid() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)], prices=[1, 1], total_lp=200
    )
    del values["assets"]["bb" * 28 + "02"]
    with pytest.raises(InvalidPoolError):
        SundaeV4Vault.model_validate(values)


def test_rider_tokens_are_tolerated() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)], prices=[1, 1], total_lp=200
    )
    values["assets"]["dd" * 28 + "ff"] = 7
    vault = SundaeV4Vault.model_validate(values)
    assert "dd" * 28 + "ff" not in vault.reserves.root
    assert vault.assets["dd" * 28 + "ff"] == 7


def test_selectors_follow_the_selected_network() -> None:
    preview = SundaeV4Deployment.for_network("preview")
    preprod = SundaeV4Deployment.for_network("preprod")
    SundaeV4Vault.select_network("preprod")
    assert SundaeV4Vault.pool_selector().addresses == [preprod.pool_address.encode()]
    assert SundaeV4Vault.order_selector() == [preprod.order_address.encode()]
    SundaeV4Vault.select_network("preview")
    assert SundaeV4Vault.pool_selector().addresses == [preview.pool_address.encode()]
    assert SundaeV4Vault.dex() == "SundaeSwapV4"


class _RedeemerBackend(_Minimal):
    """Serves the fixture's recorded redeemers for one transaction."""

    def __init__(self, records: dict[str, list[RedeemerRecord]]) -> None:
        self.records = records
        self.calls = 0

    def get_redeemers(self, tx_hash: str) -> list[RedeemerRecord]:
        self.calls += 1
        return self.records.get(tx_hash, [])


def _create_records(rec: dict) -> list[RedeemerRecord]:
    deployment = SundaeV4Deployment.for_network(rec["network"])
    return [
        RedeemerRecord(
            tx_hash=rec["tx"],
            purpose="reward",
            index=i,
            script_hash=deployment.validator(f"{kind}.withdraw").hex(),
            data_cbor=cbor,
        )
        for i, (kind, cbor) in enumerate(rec["module_create_redeemers"].items())
    ]


_CREATED = [
    pytest.param(rec, id=rec["label"])
    for rec in _FIX["pool_datums"]
    if rec.get("module_create_redeemers")
]


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    SundaeV4Vault.clear_config_cache()


@pytest.mark.parametrize("rec", _CREATED)
def test_every_committed_config_resolves_from_the_create_redeemers(rec: dict) -> None:
    set_backend(_RedeemerBackend({rec["tx"]: _create_records(rec)}))
    vault = _vault(rec)
    deployment = SundaeV4Deployment.for_network(rec["network"])
    for kind in ("constant_sum", "fee_split", "governance", "treasury_policy"):
        module = deployment.validator(f"{kind}.withdraw")
        config = vault.module_config(module)
        assert config is not None
        assert module_config_hash(config) == vault.module_state[module]
    assert isinstance(
        vault.module_config(deployment.validator("constant_sum.withdraw")),
        ConstantSumConfig,
    )
    assert isinstance(
        vault.module_config(deployment.validator("fee_split.withdraw")),
        FeeSplitConfig,
    )
    assert isinstance(
        vault.module_config(deployment.validator("governance.withdraw")),
        RawPlutusData,
    )
    assert vault.module_config(deployment.validator("fairness.withdraw")) is None


@pytest.mark.parametrize("rec", _CREATED)
def test_constant_sum_config_resolves_from_an_operate_redeemer(rec: dict) -> None:
    deployment = SundaeV4Deployment.for_network(rec["network"])
    cs = deployment.validator("constant_sum.withdraw")
    scoop = (
        next(
            s
            for s in _FIX["scoops"]
            if any(ps["pool_input"]["tx"] == rec["tx"] for ps in s["pool_spends"])
        )
        if any(
            ps["pool_input"]["tx"] == rec["tx"]
            for s in _FIX["scoops"]
            for ps in s["pool_spends"]
        )
        else None
    )
    if scoop is None:
        pytest.skip("no recorded scoop spends this pool state")
    record = RedeemerRecord(
        tx_hash=rec["tx"],
        purpose="reward",
        index=0,
        script_hash=cs.hex(),
        data_cbor=scoop["withdraw_redeemers"]["constant_sum"],
    )
    set_backend(_RedeemerBackend({rec["tx"]: [record]}))
    vault = _vault(rec)
    config = vault.module_config(cs)
    assert isinstance(config, ConstantSumConfig)
    assert module_config_hash(config) == vault.module_state[cs]


def test_supplied_config_is_verified_against_the_commitment() -> None:
    values, config = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)],
        prices=[1, 1],
        total_lp=200,
    )
    vault = SundaeV4Vault.model_validate(values)
    cs = SundaeV4Deployment.for_network("preview").validator("constant_sum.withdraw")
    wrong = ConstantSumConfig.from_cbor(config.to_cbor())
    wrong.fee.num = 7
    with pytest.raises(InvalidPoolError):
        vault.supply_module_config(cs, wrong)
    vault.supply_module_config(cs, config)
    assert vault.module_config(cs) is config


def test_configs_may_be_supplied_at_construction() -> None:
    values, config = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)],
        prices=[1, 1],
        total_lp=200,
    )
    cs = SundaeV4Deployment.for_network("preview").validator("constant_sum.withdraw")
    values["module_configs"] = {cs: config}
    set_backend(_RedeemerBackend({}))
    vault = SundaeV4Vault.model_validate(values)
    assert vault.module_config(cs) is config


def test_cache_is_keyed_by_config_hash_across_pools() -> None:
    a, config = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)],
        prices=[1, 1],
        total_lp=200,
        identifier=b"\x01" * 28,
    )
    b, _ = build_vault_utxo(
        [("aa" * 28 + "01", 5), ("bb" * 28 + "02", 9)],
        prices=[1, 1],
        total_lp=14,
        identifier=b"\x02" * 28,
    )
    b["tx_hash"] = "ef" * 32
    cs = SundaeV4Deployment.for_network("preview").validator("constant_sum.withdraw")
    create = ConstantSumCreate(initial_state=config, pool_output_index=0)
    backend = _RedeemerBackend(
        {
            a["tx_hash"]: [
                RedeemerRecord(
                    tx_hash=a["tx_hash"],
                    purpose="reward",
                    index=0,
                    script_hash=cs.hex(),
                    data_cbor=create.to_cbor().hex(),
                )
            ]
        },
    )
    set_backend(backend)
    first = SundaeV4Vault.model_validate(a).module_config(cs)
    second = SundaeV4Vault.model_validate(b).module_config(cs)
    assert backend.calls == 1
    assert module_config_hash(first) == module_config_hash(second)


def test_unresolvable_config_raises_module_config_unavailable() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)],
        prices=[1, 1],
        total_lp=200,
    )
    cs = SundaeV4Deployment.for_network("preview").validator("constant_sum.withdraw")
    set_backend(_RedeemerBackend({}))
    with pytest.raises(ModuleConfigUnavailableError):
        SundaeV4Vault.model_validate(values).module_config(cs)
    set_backend(_Minimal())
    with pytest.raises(ModuleConfigUnavailableError):
        SundaeV4Vault.model_validate(values).module_config(cs)


def test_unknown_module_hash_is_a_key_error() -> None:
    values, _ = build_vault_utxo(
        [("aa" * 28 + "01", 100), ("bb" * 28 + "02", 100)],
        prices=[1, 1],
        total_lp=200,
    )
    with pytest.raises(KeyError):
        SundaeV4Vault.model_validate(values).module_config(b"\x00" * 28)
