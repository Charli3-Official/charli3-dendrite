"""SundaeV4Vault: the pool UTxO primitive parsed from value + datum."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import pool_identifier
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.dexs.core.errors import NotAPoolError
from tests.sundae_v4_vault_factory import build_vault_utxo

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
