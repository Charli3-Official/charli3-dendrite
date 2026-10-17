"""The bundled Liqwid V2 deployment snapshot (``registry_v6.json``)."""

import importlib.util
import json
import subprocess
import sys
from datetime import date
from datetime import datetime
from datetime import timezone
from importlib.resources import files
from pathlib import Path

import pytest

from charli3_dendrite.lending.liqwid import deployment
from charli3_dendrite.lending.liqwid.deployment import FIRST_ORACLE_SLOT
from charli3_dendrite.lending.liqwid.deployment import FIRST_SLOT
from charli3_dendrite.lending.liqwid.deployment import MARKET_FAMILIES
from charli3_dendrite.lending.liqwid.deployment import ORACLE_TOKEN_POLICY
from charli3_dendrite.lending.liqwid.deployment import POSIX_SLOT_OFFSET
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import OracleValidator
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.deployment import load_deployment

PACKAGE = "charli3_dendrite.lending.liqwid"
UNKNOWN = "00" * 28
LAUNCH = {
    "Ada": "0fd1b854729d2e29290d47fe2173319aae3c4c885a955dc203f72759",
    "DJED": "63d65418f841e9901d5ee634cc07377cca2be7fb2b797e3f0e0d8334",
    "SHEN": "89ea1358d8147feb5ef1875fbad449f65719ced6102ce1600bf093a7",
    "IUSD": "9885c2bc619cb1e09d63c59eebef3c27e7b796cdca3ff89fdbfb4fa1",
}
# (AnastasiaAudit BorrowStaking, AnastasiaAudit LiquidationStaking) per market.
ANASTASIA_AUDIT_STAKING = {
    "ETH": (
        "d83f2ac9ebb6e8c7eac02e534ad3425a4f333792960605678b5ca788",
        "a1c36d24c965f213b21b06bf111549de9b05c261faa78e08efdfdafe",
    ),
    "DAI": (
        "46c973abc0217004ee349de0d58f5f131f60b0e57e92af21df24c606",
        "0e338e00b1d640fea8dffe1f4aea1a6a79c6633e200fae67aef93b7d",
    ),
    "USDT": (
        "ea5c6035bcfd83a92c04eff0008b6fd02efac6a20e0abdaebaee2808",
        "afd0199edce0910b2d4ba9cf97e373d342b8de8ca2a2b764538a4dae",
    ),
}
REFRESH_SCRIPT = Path(__file__).parent / "fixtures" / "_refresh_registry.py"


def _bundled() -> dict:
    return json.loads((files(PACKAGE) / "registry_v6.json").read_text())


def _by_name() -> dict[str, MarketComponents]:
    return {market.name: market for market in default_deployment().markets}


def test_snapshot_has_35_markets_sorted_by_name():
    names = [market.name for market in default_deployment().markets]
    assert len(names) == 35
    assert names == sorted(names)
    assert {"Ada", "DJED", "SHEN", "IUSD", "ETH", "SNEK2-ADA", "STRIKE-ADA"} <= set(
        names,
    )


def test_validator_totals_match_the_chain():
    markets = default_deployment().markets
    assert sum(len(m.loan_validators) for m in markets) == 69
    assert sum(len(m.legacy_loan_validators) for m in markets) == 35
    assert sum(len(m.new_loan_validators) for m in markets) == 34
    assert sum(len(m.market_state_validators) for m in markets) == 41
    assert len({a for m in markets for a in m.actions}) == 95


def test_launch_validators_sit_first_under_their_market():
    markets = _by_name()
    for name, validator in LAUNCH.items():
        assert markets[name].market_state_validators[0] == validator
        owners = [
            m.name for m in markets.values() if validator in m.market_state_validators
        ]
        assert owners == [name]
    # Ada moved off its launch validator on 2023-07-19.
    assert markets["Ada"].market_state_validators == (
        LAUNCH["Ada"],
        "8d258b9d08dcab73f3165a11751d464b46056264091c1789da588726",
    )


def test_family_table_covers_every_role():
    assert set(MARKET_FAMILIES) == {
        ("MarketState", "MintingPolicy"),
        ("MarketParams", "MintingPolicy"),
        ("CollateralParams", "MintingPolicy"),
        ("QToken", "MintingPolicy"),
        ("Borrow", "MintingPolicy"),
        ("NewBorrow", "MintingPolicy"),
        ("Liquidation", "MintingPolicy"),
        ("MarketState", "Validator"),
        ("AnastasiaAuditMarketState", "Validator"),
        ("Loan", "Validator"),
        ("NewLoanBorrowStaking", "StakeValidator"),
        ("AnastasiaAuditLoanBorrowStaking", "StakeValidator"),
        ("NewLoanLiquidationStaking", "StakeValidator"),
        ("AnastasiaAuditLoanLiquidationStaking", "StakeValidator"),
        ("Action", "Validator"),
        ("AnastasiaAuditAction", "Validator"),
        ("Batch", "Validator"),
        ("AnastasiaAuditBatch", "Validator"),
        ("BatchFinal", "Validator"),
        ("AnastasiaAuditBatchFinal", "Validator"),
    }


def test_every_snapshot_hash_lands_in_its_role():
    markets = _by_name()
    seen_roles = set()
    for entry in _bundled()["scriptInfos"]:
        if "market" not in entry:
            continue
        market = markets[entry["market"]]
        role = MARKET_FAMILIES[(entry["componentName"], entry["type"])]
        if role == "loan_validators":
            role = (
                "new_loan_validators"
                if entry["name"].endswith("NewLoanValidator")
                else "legacy_loan_validators"
            )
        value = getattr(market, role)
        held = value if isinstance(value, tuple) else (value,)
        assert entry["scriptHash"] in held, (market.name, role)
        seen_roles.add(role)
    assert seen_roles == {
        "market_state_policy",
        "market_params_policy",
        "collateral_params_policy",
        "qtoken_policy",
        "borrow_policy",
        "new_borrow_policy",
        "liquidation_policy",
        "market_state_validators",
        "legacy_loan_validators",
        "new_loan_validators",
        "borrow_staking",
        "liquidation_staking",
        "actions",
        "batches",
        "batch_finals",
    }


def test_every_market_has_its_single_policies_and_generations():
    for market in default_deployment().markets:
        assert len(market.market_id) == 56
        assert market.borrow_policy is not None
        assert market.liquidation_policy is not None
        assert market.legacy_loan_validators
        assert market.actions
        assert market.batches
        assert market.batch_finals


def test_eth_dai_usdt_staking_includes_the_anastasia_audit_pair():
    markets = _by_name()
    for name, (borrow, liquidation) in ANASTASIA_AUDIT_STAKING.items():
        market = markets[name]
        assert len(market.borrow_staking) == 2
        assert len(market.liquidation_staking) == 2
        assert borrow in market.borrow_staking
        assert liquidation in market.liquidation_staking


def test_pol_has_only_the_legacy_generation():
    pol = _by_name()["POL"]
    assert pol.new_borrow_policy is None
    assert pol.new_loan_validators == ()
    assert pol.borrow_staking == ()
    assert pol.liquidation_staking == ()
    assert pol.loan_token_policies == frozenset({pol.borrow_policy})


def test_underlying_units_and_decimals():
    markets = _by_name()
    assert markets["Ada"].underlying == "lovelace"
    assert markets["SNEK2-ADA"].underlying == "lovelace"
    assert markets["STRIKE-ADA"].underlying == "lovelace"
    assert markets["DJED"].underlying == (
        "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
        "446a65644d6963726f555344"
    )
    assert markets["Ada"].decimals == 6
    assert markets["ETH"].decimals == 8
    assert markets["SNEK"].decimals == 0


def test_market_id_is_the_market_state_nft_policy():
    for market in default_deployment().markets:
        assert market.market_id == market.market_state_policy
    assert _by_name()["Ada"].market_id == (
        "5a3cb4f52fb3a00ab5abe62080618623811ccafab9c760d0b686e44c"
    )


def test_every_lookup_round_trips():
    dep = default_deployment()
    for market in dep.markets:
        assert dep.market(market.market_id) is market
        assert dep.market_of_state_policy(market.market_state_policy) is market
        assert dep.market_of_params_policy(market.market_params_policy) is market
        assert (
            dep.market_of_collateral_params_policy(market.collateral_params_policy)
            is market
        )
        assert dep.market_of_qtoken(market.qtoken_policy) is market
        for credential in market.legacy_loan_validators:
            assert dep.market_of_loan_validator(credential) == (market, "legacy")
        for credential in market.new_loan_validators:
            assert dep.market_of_loan_validator(credential) == (market, "new")
    assert len(dep.loan_token_policies) == 69
    assert dep.loan_token_policies == frozenset().union(
        *(m.loan_token_policies for m in dep.markets),
    )


def test_unknown_keys_give_none_or_key_error():
    dep = default_deployment()
    with pytest.raises(KeyError):
        dep.market(UNKNOWN)
    assert dep.market_of_loan_validator(UNKNOWN) is None
    assert dep.market_of_state_policy(UNKNOWN) is None
    assert dep.market_of_params_policy(UNKNOWN) is None
    assert dep.market_of_collateral_params_policy(UNKNOWN) is None
    assert dep.market_of_qtoken(UNKNOWN) is None
    # A MarketState validator is not a MarketState NFT policy.
    assert dep.market_of_state_policy(LAUNCH["Ada"]) is None


def test_deployment_wide_scripts():
    dep = default_deployment()
    assert dep.param_credential == (
        "b575da974ec756ba2afdccdc07cbd1839acaf0a24c4f80afd81d3fb6"
    )
    assert dep.oracle_validators == (
        OracleValidator(
            "v1", "21bd8c2e0df2fbe92137f78dbaba48f62308e52303049f0d628b6c4c"
        ),
        OracleValidator(
            "hard_caps",
            "38af3730fecea148e9dfa30d3fcfff573d1ea8c8e31efe73471112a1",
        ),
        OracleValidator(
            "pegs", "38cb4955f3696ba0a27a3295a0b9af8a693057f0828e5709ca1b1c41"
        ),
    )
    assert dep.oracle_token_policy == ORACLE_TOKEN_POLICY


def test_floors_and_slot_offset():
    def utc(slot: int) -> datetime:
        return datetime.fromtimestamp(slot + POSIX_SLOT_OFFSET, tz=timezone.utc)

    assert utc(FIRST_SLOT).date() == date(2023, 2, 2)
    assert utc(FIRST_ORACLE_SLOT).strftime("%Y-%m-%d %H:%M") == "2023-02-02 00:44"
    assert FIRST_ORACLE_SLOT < FIRST_SLOT


def test_default_deployment_reads_package_data_through_importlib_resources(
    monkeypatch,
):
    calls = []

    def spy(package):
        calls.append(package)
        return files(package)

    monkeypatch.setattr(deployment, "files", spy)
    deployment._bundled_registry.cache_clear()
    default_deployment.cache_clear()
    try:
        first = default_deployment()
        assert default_deployment() is first
        assert calls == [PACKAGE]
    finally:
        deployment._bundled_registry.cache_clear()
        default_deployment.cache_clear()
    assert (files(PACKAGE) / "registry_v6.json").is_file()


def test_import_reads_nothing():
    code = (
        "import charli3_dendrite.lending.liqwid.deployment as d;"
        "print(d._bundled_registry.cache_info().currsize,"
        " d.default_deployment.cache_info().currsize)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stdout.split() == ["0", "0"]


def test_a_fetched_registry_without_hand_kept_block_borrows_the_bundled_one():
    document = _bundled()
    del document["handKept"]
    assert load_deployment(document) == default_deployment()


def test_load_deployment_rejects_ambiguous_or_incomplete_registries():
    shared = _bundled()
    ada_loan = _by_name()["Ada"].legacy_loan_validators[0]
    for entry in shared["scriptInfos"]:
        if entry.get("market") == "DJED" and entry["name"] == "LiqwidDJEDLoan":
            entry["scriptHash"] = ada_loan
    with pytest.raises(ValueError, match="two Liqwid markets"):
        load_deployment(shared)

    no_nft = _bundled()
    no_nft["scriptInfos"] = [
        e
        for e in no_nft["scriptInfos"]
        if not (e.get("market") == "DJED" and e["name"] == "LiqwidDJEDMarketStateToken")
    ]
    with pytest.raises(ValueError, match="market_state_policy"):
        load_deployment(no_nft)

    stray = _bundled()
    stray["handKept"]["launchMarketStateValidators"]["NOPE"] = UNKNOWN
    with pytest.raises(ValueError, match="unknown markets"):
        load_deployment(stray)


def test_constructed_deployment_indexes_its_markets():
    ada = _by_name()["Ada"]
    dep = LiqwidDeployment((ada,), "aa" * 28, (), ORACLE_TOKEN_POLICY)
    assert dep.market(ada.market_id) is ada
    assert dep.market_of_qtoken(_by_name()["DJED"].qtoken_policy) is None


def test_refresh_script_trim_is_idempotent_on_the_snapshot():
    spec = importlib.util.spec_from_file_location("_refresh_registry", REFRESH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bundled = _bundled()
    raw = json.dumps(bundled).encode()
    trimmed = module.trim(raw)
    assert {k: v for k, v in trimmed.items() if not k.startswith("_")} == {
        k: v for k, v in bundled.items() if not k.startswith("_")
    }
    assert module.HAND_KEPT == bundled["handKept"]
    allowed = {"tag", "market", "componentName", "name", "type", "scriptHash"}
    assert all(set(entry) <= allowed for entry in bundled["scriptInfos"])
