"""The bundled Liqwid V2 deployment snapshot (``registry_v6.json``)."""

import decimal
import importlib.util
import json
import subprocess
import sys
from datetime import date
from datetime import datetime
from datetime import timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

import cbor2
import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.liqwid import deployment
from charli3_dendrite.lending.liqwid import indexing
from charli3_dendrite.lending.liqwid import lifecycle
from charli3_dendrite.lending.liqwid import market as liqwid_market
from charli3_dendrite.lending.liqwid import oracles
from charli3_dendrite.lending.liqwid.deployment import BURN_CREDENTIAL
from charli3_dendrite.lending.liqwid.deployment import FIRST_ORACLE_SLOT
from charli3_dendrite.lending.liqwid.deployment import FIRST_SLOT
from charli3_dendrite.lending.liqwid.deployment import MARKET_COMPONENT_ROLES
from charli3_dendrite.lending.liqwid.deployment import MARKET_FAMILIES
from charli3_dendrite.lending.liqwid.deployment import MARKET_ROLE_FIELDS
from charli3_dendrite.lending.liqwid.deployment import ORACLE_TOKEN_POLICY
from charli3_dendrite.lending.liqwid.deployment import POSIX_SLOT_OFFSET
from charli3_dendrite.lending.liqwid.deployment import (
    UNREGISTERED_MARKET_STATE_VALIDATORS,
)
from charli3_dendrite.lending.liqwid.deployment import DriftFinding
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import OracleValidator
from charli3_dendrite.lending.liqwid.deployment import check_deployment
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.deployment import load_deployment
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.lifecycle import LoanKey
from charli3_dendrite.lending.liqwid.lifecycle import LoanTransition
from charli3_dendrite.lending.units import script_credential
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.liqwid.records import API
from tests.lending.liqwid.records import CONTEXT_KINDS
from tests.lending.liqwid.records import RECORDS
from tests.lending.liqwid.records import SPEND_RULES
from tests.lending.liqwid.records import UTXO_KINDS
from tests.lending.liqwid.records import context_at
from tests.lending.liqwid.records import live_context
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.liqwid.records import spend_of

PACKAGE = "charli3_dendrite.lending.liqwid"
UNKNOWN = "01" * 28
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
        return datetime.fromtimestamp(slot_to_posix_ms(slot) // 1000, tz=timezone.utc)

    # The offset is the shared mainnet slot anchor, in seconds.
    for slot in (FIRST_ORACLE_SLOT, FIRST_SLOT, 0):
        assert slot + POSIX_SLOT_OFFSET == slot_to_posix_ms(slot) // 1000
    assert POSIX_SLOT_OFFSET == 1_591_566_291

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


# --- captured mainnet records (fixtures/records.json.gz, fixtures/api_snapshot.json)

# One MarketParams output per datum layout, keyed by the list length.
MARKET_PARAMS_LAYOUTS = {
    27: "36d4bfe57e1ebd90e0e586f228b84903403e72c9d6d54b88994f66a0372f18ee#1",
    30: "8eb900a137226047109f92f1bd1c73d18d3ce1b55ba621345ffc9acb727d9f56#0",
    32: "e51f0164dba6c22842e7ae8f539582c2e5b425ea6ac2e824beaa7bfb29665fba#3",
    34: "6e8dab3ab0d52a00f848ec209ec6fd37504afa4076d1bbfc5abccf85d061d9da#10",
    36: "52474aff233de3dc5a75b80de64ceb4ae63aabb99cd7bd408c51349979ed9a2b#0",
}
PARKED_MARKET_PARAMS = (
    "88d368eaf1347e4b8643e17983dff1826252b38e0bc740f7c65bd2d7c3651f62#0"
)
DEAD_COLLATERAL_PARAMS_PREFIX = "90316fe4"
REJECT_LABELS = {
    "oracle_decoy",
    "param_dust",
    "dead_policy_collateral_params",
    "parked_market_params",
    "parking_loan",
    "datumless_loan",
    "state_list0",
    "state_list27",
}


def _credential(rec):
    credential = script_credential(rec["address"])
    assert credential is not None, rec["out_ref"]
    return credential


def _policies(rec):
    return {unit[:56] for unit in rec["assets"] if unit != "lovelace"}


def _script_class_policies():
    """Policies named by script-class (``C0[policy]``) CollateralParams records."""
    policies = set()
    for rec in records_of("collateral_params"):
        if rec["label"] is not None:
            continue
        collateral = cbor2.loads(bytes.fromhex(rec["datum_cbor"]))[0]
        if collateral.tag == 121:
            policies.add(collateral.value[0].hex())
    return policies


def _every_loan_output():
    spends = [
        rec for spend in RECORDS["spends"] for rec in spend["spent"] + spend["created"]
    ]
    return records_of("loan") + spends


def test_captured_entries_carry_consistent_chain_points():
    for kind in UTXO_KINDS:
        entries = records_of(kind)
        assert entries, kind
        out_refs = [rec["out_ref"] for rec in entries]
        assert out_refs == sorted(set(out_refs)), kind
        for rec in entries:
            posix_s = slot_to_posix_ms(rec["slot"]) // 1000
            assert rec["block_time"] == posix_s, rec["out_ref"]
            spent = rec["spent_slot"]
            assert spent is None or spent >= rec["slot"], rec["out_ref"]
            alive = rec["slot"] <= API["slot"] and (
                spent is None or spent > API["slot"]
            )
            assert rec["live"] is alive, rec["out_ref"]


def test_captured_records_start_at_the_deployment_floors():
    assert min(rec["slot"] for rec in records_of("oracle")) == FIRST_ORACLE_SLOT
    genuine = [
        rec
        for kind in ("market_state", "market_params", "collateral_params")
        for rec in records_of(kind)
        if rec["label"] is None
    ]
    assert min(rec["slot"] for rec in genuine) == FIRST_SLOT


def test_every_market_params_layout_and_non_record_is_captured():
    by_ref = {rec["out_ref"]: rec for rec in records_of("market_params")}
    for length, out_ref in MARKET_PARAMS_LAYOUTS.items():
        rec = by_ref[out_ref]
        assert rec["label"] is None
        assert len(cbor2.loads(bytes.fromhex(rec["datum_cbor"]))) == length
    assert by_ref[PARKED_MARKET_PARAMS]["label"] == "parked_market_params"
    rejects = records_of("reject")
    assert {rec["label"] for rec in rejects} == REJECT_LABELS
    assert len([rec for rec in rejects if rec["label"] == "oracle_decoy"]) == 4


def test_snapshot_covers_every_captured_market_state():
    deployment = default_deployment()
    for rec in records_of("market_state"):
        markets = {deployment.market_of_state_policy(p) for p in _policies(rec)}
        (market,) = markets - {None}
        assert rec["market_id"] == market.market_id, rec["out_ref"]
        assert _credential(rec) in market.market_state_validators, rec["out_ref"]
    live = {
        rec["market_id"]
        for rec in records_of("market_state", live=True)
        if rec["label"] is None
    }
    assert live == {market.market_id for market in deployment.markets}


def test_snapshot_covers_every_captured_parameter_record():
    deployment = default_deployment()
    for rec in records_of("market_params"):
        assert _credential(rec) == deployment.param_credential, rec["out_ref"]
        markets = {deployment.market_of_params_policy(p) for p in _policies(rec)}
        if rec["label"] == "param_dust":
            assert markets <= {None}, rec["out_ref"]
            continue
        (market,) = markets - {None}
        assert rec["market_id"] == market.market_id, rec["out_ref"]
    dead = []
    for rec in records_of("collateral_params"):
        assert _credential(rec) == deployment.param_credential, rec["out_ref"]
        markets = {
            deployment.market_of_collateral_params_policy(p) for p in _policies(rec)
        }
        if rec["label"] == "dead_policy_collateral_params":
            assert markets <= {None}, rec["out_ref"]
            assert any(
                p.startswith(DEAD_COLLATERAL_PARAMS_PREFIX) for p in _policies(rec)
            )
            dead.append(rec)
            continue
        (market,) = markets - {None}
        assert rec["market_id"] == market.market_id, rec["out_ref"]
    assert dead


def test_snapshot_covers_every_captured_loan_output():
    deployment = default_deployment()
    script_class = _script_class_policies()
    assert script_class
    holds_script_class = False
    for rec in _every_loan_output():
        found = deployment.market_of_loan_validator(_credential(rec))
        assert found is not None, rec["out_ref"]
        market, generation = found
        assert (rec["market_id"], rec["generation"]) == (market.market_id, generation)
        if rec["label"] is not None:
            continue
        for policy in _policies(rec):
            if policy in script_class:
                holds_script_class = True
                continue
            assert (
                policy in deployment.loan_token_policies
                or deployment.market_of_qtoken(policy) is not None
            ), (rec["out_ref"], policy)
    assert holds_script_class


def test_snapshot_covers_every_captured_oracle_output():
    deployment = default_deployment()
    assert deployment.oracle_token_policy == ORACLE_TOKEN_POLICY
    roles = {v.script_hash: v.role for v in deployment.oracle_validators}
    seen = set()
    for rec in records_of("oracle"):
        role = roles.get(_credential(rec))
        assert role is not None, rec["out_ref"]
        assert (rec["market_id"], rec["generation"]) == (None, None)
        if rec["label"] == "oracle_decoy":
            assert role == "hard_caps", rec["out_ref"]
            assert not _policies(rec), rec["out_ref"]
            continue
        assert _policies(rec) == {ORACLE_TOKEN_POLICY}, rec["out_ref"]
        assert rec["assets"][ORACLE_TOKEN_POLICY] == 1, rec["out_ref"]
        seen.add(role)
    assert seen == {"v1", "hard_caps", "pegs"}


def test_spends_cover_every_lifecycle_rule_once():
    assert sorted(spend["rule"] for spend in RECORDS["spends"]) == sorted(SPEND_RULES)
    assert {n for n in SPEND_RULES.values() if n is not None} == set(range(1, 9))
    transitions = {transition.value for transition in LoanTransition}
    for rule in SPEND_RULES:
        spend = spend_of(rule)
        assert spend["spent"], rule
        assert set(spend["expected"]) == {rec["out_ref"] for rec in spend["spent"]}
        created = {rec["out_ref"] for rec in spend["created"]}
        for outcome in spend["expected"].values():
            assert outcome["transition"] in transitions, rule
            assert outcome["successor"] is None or outcome["successor"] in created
    with pytest.raises(KeyError):
        spend_of("no_such_rule")


def test_spend_evidence_names_bundled_scripts():
    deployment = default_deployment()

    def market_of(rule):
        (market_id,) = {rec["market_id"] for rec in spend_of(rule)["spent"]}
        return deployment.market(market_id)

    staking = (
        ("partial_liquidation", market_of("partial_liquidation").liquidation_staking),
        ("new_modification", market_of("new_modification").borrow_staking),
        ("full_repay", market_of("full_repay").borrow_staking),
    )
    for rule, scripts in staking:
        assert set(spend_of(rule)["withdrawals"]) & set(scripts), rule
    liquidation = market_of("legacy_liquidation").liquidation_policy
    assert spend_of("legacy_liquidation")["minted"][liquidation] > 0
    repay = market_of("legacy_repay").borrow_policy
    assert repay in spend_of("legacy_repay")["burned_any"]
    flip = market_of("legacy_flip").borrow_policy
    assert flip in spend_of("legacy_flip")["burned_any"]
    assert spend_of("legacy_flip")["minted"][flip] == 0


def test_max_borrows_reference_their_market_records():
    deployment = default_deployment()
    names = set()
    for entry in RECORDS["max_borrows"]:
        market = deployment.market(entry["market_id"])
        names.add(market.name)
        assert market.market_state_policy in _policies(entry["state"])
        assert market.market_params_policy in _policies(entry["params"])
        assert entry["state"]["slot"] <= entry["slot"]
        assert entry["params"]["slot"] <= entry["slot"]
        assert entry["borrowed"] > 0
    assert names == {"USDM", "STRIKE-ADA", "DJED"}


def test_api_snapshot_matches_the_captured_live_records():
    deployment = default_deployment()
    live_loans = {
        rec["out_ref"] for rec in records_of("loan", live=True) if rec["label"] is None
    }
    assert set(API["loans"]) == live_loans
    assert len(API["markets"]) == 35
    assert set(API["markets"]) == {market.market_id for market in deployment.markets}
    for values in API["loans"].values():
        assert set(values) == {"debt", "interest", "healthFactor", "LTV"}
    for values in API["markets"].values():
        assert set(values) == {
            "borrowAPR",
            "supplyAPR",
            "utilization",
            "exchangeRate",
            "liquidity",
            "borrowCap",
        }


def test_record_helpers_build_contexts_from_the_captured_records():
    live = live_context()
    assert live.slot == API["slot"]
    live_refs = {f"{info.tx_hash}#{info.tx_index}" for info in live.utxos}
    assert live_refs == {
        rec["out_ref"] for kind in CONTEXT_KINDS for rec in records_of(kind, live=True)
    }
    assert not live_refs & {rec["out_ref"] for rec in records_of("loan")}

    genesis = min(
        (rec for rec in records_of("market_state") if rec["label"] is None),
        key=lambda rec: (rec["slot"], rec["block_index"]),
    )
    info = record_info(genesis)
    assert (info.block_time, info.block_index, info.datum_cbor) == (
        genesis["block_time"],
        genesis["block_index"],
        genesis["datum_cbor"],
    )
    assert record_info(genesis, block_time=1).block_time == 1
    early = context_at(genesis["slot"] + 1)
    assert early.slot == genesis["slot"] + 1
    early_refs = {f"{info.tx_hash}#{info.tx_index}" for info in early.utxos}
    assert genesis["out_ref"] in early_refs
    assert all(info.block_time <= genesis["block_time"] for info in early.utxos)
    # Oracle feeds predate the first market; no genuine market record does.
    before = {f"{i.tx_hash}#{i.tx_index}" for i in context_at(genesis["slot"]).utxos}
    market_records = {
        rec["out_ref"]
        for kind in ("market_state", "market_params", "collateral_params")
        for rec in records_of(kind)
        if rec["label"] is None
    }
    assert not before & market_records


def test_every_captured_historical_state_has_its_parameters():
    params = {
        rec["out_ref"]: rec["market_id"]
        for rec in records_of("market_params")
        if rec["label"] is None
    }
    states = [
        rec for rec in records_of("market_state", live=False) if rec["label"] is None
    ]
    assert states
    for state in states:
        context = context_at(state["slot"] + 1)
        held = {f"{info.tx_hash}#{info.tx_index}" for info in context.utxos}
        markets = {params[ref] for ref in held if ref in params}
        assert state["market_id"] in markets, state["out_ref"]


# --- Deployment drift --------------------------------------------------------

# STRIKE-ADA's MarketState NFT policy: the token that moved validators on 2026-08-07.
STRIKE_ADA_STATE_NFT = "b44553158796da29abd7d74ebd61cfe64274d00b72962acc6b5befba"


def _params_market(rec: dict[str, Any]) -> MarketComponents | None:
    """The bundled market whose MarketParams NFT the captured output holds."""
    for policy in _policies(rec):
        market = default_deployment().market_of_params_policy(policy)
        if market is not None:
            return market
    return None


def _live_params() -> list[PoolStateInfo]:
    return [record_info(rec) for rec in records_of("market_params", live=True)]


def _live_layout36() -> tuple[dict[str, Any], list[Any], MarketComponents]:
    """A captured live list36 MarketParams of a bundled market, its fields, market."""
    for rec in records_of("market_params", live=True):
        if rec["label"] is not None:  # param dust / parked outputs share the list
            continue
        fields = cbor2.loads(bytes.fromhex(rec["datum_cbor"]))
        market = _params_market(rec)
        if isinstance(fields, list) and len(fields) == 36 and market is not None:
            return rec, fields, market
    raise AssertionError("no captured live list36 MarketParams")


def _observed_pairs() -> list[tuple[str, str]]:
    """Every (payment credential, policy) pair held by a captured record."""
    pairs: set[tuple[str, str]] = set()
    for kind in UTXO_KINDS:
        for rec in records_of(kind):
            credential = script_credential(rec["address"])
            if credential is not None:
                pairs.update((credential, policy) for policy in _policies(rec))
    return sorted(pairs)


def _keys(findings: list[DriftFinding]) -> list[tuple[str, str | None, str, str]]:
    return [(f.kind, f.market, f.component, f.script_hash) for f in findings]


def test_market_component_roles_fill_every_tracked_field() -> None:
    assert MARKET_COMPONENT_ROLES is MARKET_FAMILIES
    assert set(MARKET_COMPONENT_ROLES.values()) == set(MARKET_ROLE_FIELDS)
    for market in default_deployment().markets:
        for name in MARKET_ROLE_FIELDS:
            getattr(market, name)  # every role is a MarketComponents attribute


def test_unregistered_validators_are_the_hand_kept_launch_validators() -> None:
    hand_kept = _bundled()["handKept"]["launchMarketStateValidators"]
    assert UNREGISTERED_MARKET_STATE_VALIDATORS == set(hand_kept.values())
    assert UNREGISTERED_MARKET_STATE_VALIDATORS == set(LAUNCH.values())


def test_observed_pairs_cover_every_identity_kind() -> None:
    deployment = default_deployment()
    policies = {policy for _, policy in _observed_pairs()}
    assert deployment.oracle_token_policy in policies
    assert policies & deployment.loan_token_policies
    assert policies & {m.market_state_policy for m in deployment.markets}


def test_no_drift_on_captured_data() -> None:
    params = _live_params()
    assert params
    assert (
        check_deployment(
            params,
            registry=_bundled(),
            observed_credentials=_observed_pairs(),
        )
        == []
    )


def test_no_input_no_findings() -> None:
    assert check_deployment() == []
    assert check_deployment(deployment=default_deployment()) == []


def test_unknown_p35_loan_validator_is_one_params_finding() -> None:
    rec, fields, market = _live_layout36()
    fields[35] = bytes.fromhex(UNKNOWN)
    info = record_info(rec, datum_cbor=cbor2.dumps(fields).hex())
    findings = check_deployment([info])
    assert _keys(findings) == [("params", market.name, "loan_validators", UNKNOWN)]
    assert rec["out_ref"] in findings[0].detail
    assert "p[35]" in findings[0].detail


def test_unknown_p34_staking_hash_is_one_params_finding() -> None:
    rec, fields, market = _live_layout36()
    pair = list(cbor2.loads(cbor2.dumps(fields[34])))
    first = pair[0]
    pair[0] = cbor2.CBORTag(first.tag, [bytes.fromhex(UNKNOWN)])
    fields[34] = pair
    info = record_info(rec, datum_cbor=cbor2.dumps(fields).hex())
    assert _keys(check_deployment([info])) == [
        ("params", market.name, "staking", UNKNOWN),
    ]


@pytest.mark.parametrize("length", [28, 37])
def test_unknown_params_layout_is_one_params_finding(length: int) -> None:
    rec, fields, market = _live_layout36()
    resized = fields[:length] if length < len(fields) else [*fields, 0]
    info = record_info(rec, datum_cbor=cbor2.dumps(resized).hex())
    findings = check_deployment([info])
    assert _keys(findings) == [
        ("params", market.name, "layout", market.market_params_policy),
    ]
    assert f"{length} fields" in findings[0].detail


@pytest.mark.parametrize("datum", ["", "00", "zz", "d87a80", "d8799f"])
def test_unreadable_params_datum_is_one_params_finding(datum: str) -> None:
    rec, _, market = _live_layout36()
    findings = check_deployment([record_info(rec, datum_cbor=datum)])
    assert _keys(findings) == [
        ("params", market.name, "datum", market.market_params_policy),
    ]


def test_too_deeply_nested_params_datum_is_one_params_finding() -> None:
    rec, _, market = _live_layout36()
    nested = "81" * 100_000 + "00"
    findings = check_deployment([record_info(rec, datum_cbor=nested)])
    assert _keys(findings) == [
        ("params", market.name, "datum", market.market_params_policy),
    ]


@pytest.mark.parametrize("parked", ["d87980", "d8799fff"])
def test_parked_params_output_is_skipped(parked: str) -> None:
    rec, _, _ = _live_layout36()
    assert check_deployment([record_info(rec, datum_cbor=parked)]) == []


def test_utxo_without_a_params_nft_is_skipped() -> None:
    rec, _, _ = _live_layout36()
    dust = record_info(
        rec, datum_cbor="00", assets=Assets(root={"lovelace": 2_000_000})
    )
    assert check_deployment([dust]) == []


def test_wrong_typed_params_field_is_one_params_finding() -> None:
    rec, fields, market = _live_layout36()
    fields[35] = 7  # an int where the loan validator hash belongs
    info = record_info(rec, datum_cbor=cbor2.dumps(fields).hex())
    assert _keys(check_deployment([info])) == [
        ("params", market.name, "datum", market.market_params_policy),
    ]


def test_registry_extra_market_is_one_registry_finding() -> None:
    registry = _bundled()
    registry["scriptInfos"].append(
        {
            "tag": "liqwidMainnet",
            "market": "NEWMKT",
            "componentName": "MarketState",
            "type": "MintingPolicy",
            "scriptHash": "cd" * 28,
        },
    )
    assert _keys(check_deployment(registry=registry)) == [
        ("registry", "NEWMKT", "market", "cd" * 28),
    ]


def test_registry_changed_hash_reports_both_sides() -> None:
    registry = _bundled()
    entry = next(
        e
        for e in registry["scriptInfos"]
        if e.get("market") == "Ada" and e.get("componentName") == "NewBorrow"
    )
    old = entry["scriptHash"]
    entry["scriptHash"] = UNKNOWN
    assert sorted(_keys(check_deployment(registry=registry))) == sorted(
        [
            ("registry", "Ada", "new_borrow_policy", old),
            ("registry", "Ada", "new_borrow_policy", UNKNOWN),
        ],
    )


def test_registry_untracked_component_is_one_registry_finding() -> None:
    registry = _bundled()
    registry["scriptInfos"].append(
        {
            "tag": "liqwidMainnet",
            "market": "Ada",
            "componentName": "New2Loan",
            "type": "Validator",
            "scriptHash": "ef" * 28,
        },
    )
    assert _keys(check_deployment(registry=registry)) == [
        ("registry", "Ada", "New2Loan", "ef" * 28),
    ]


def test_registry_without_a_bundled_market_is_a_registry_finding() -> None:
    registry = _bundled()
    registry["scriptInfos"] = [
        e for e in registry["scriptInfos"] if e.get("market") != "Ada"
    ]
    ada = _by_name()["Ada"]
    assert _keys(check_deployment(registry=registry)) == [
        ("registry", "Ada", "market", ada.market_state_policy),
    ]


def test_empty_registry_reports_every_market_and_oracle_component() -> None:
    deployment = default_deployment()
    findings = check_deployment(registry={"scriptInfos": []})
    markets = {f.market for f in findings if f.component == "market"}
    assert markets == {m.name for m in deployment.markets}
    oracle = {f.script_hash for f in findings if f.market is None}
    assert oracle == {v.script_hash for v in deployment.oracle_validators} | {
        deployment.oracle_token_policy,
    }
    assert not {f.script_hash for f in findings} & UNREGISTERED_MARKET_STATE_VALIDATORS


def test_market_state_nft_at_unlisted_credential_is_identity_drift() -> None:
    deployment = default_deployment()
    market = deployment.market_of_state_policy(STRIKE_ADA_STATE_NFT)
    assert market is not None
    listed = [(cred, STRIKE_ADA_STATE_NFT) for cred in market.market_state_validators]
    assert check_deployment(observed_credentials=listed) == []
    findings = check_deployment(
        observed_credentials=[*listed, (UNKNOWN, STRIKE_ADA_STATE_NFT)],
    )
    assert _keys(findings) == [
        ("identity", market.name, "market_state_validators", UNKNOWN),
    ]
    assert STRIKE_ADA_STATE_NFT in findings[0].detail


def test_loan_and_oracle_tokens_at_unlisted_credentials_are_identity_drift() -> None:
    deployment = default_deployment()
    market = _by_name()["Ada"]
    observed = [(UNKNOWN, policy) for policy in sorted(market.loan_token_policies)]
    observed.append((UNKNOWN, deployment.oracle_token_policy))
    observed.extend(
        (validator.script_hash, deployment.oracle_token_policy)
        for validator in deployment.oracle_validators
    )
    observed.extend(
        (credential, policy)
        for credential in market.loan_validators
        for policy in market.loan_token_policies
    )
    findings = check_deployment(observed_credentials=observed)
    # A deployment-wide finding (market None) sorts before every market's.
    assert _keys(findings) == [
        ("identity", None, "oracle_validators", UNKNOWN),
        *[("identity", "Ada", "loan_validators", UNKNOWN)]
        * len(market.loan_token_policies),
    ]
    assert {f.detail for f in findings[1:]} == {
        f"identity policy {policy} at an unlisted credential"
        for policy in market.loan_token_policies
    }


def test_untracked_policy_is_not_identity_drift() -> None:
    assert check_deployment(observed_credentials=[(UNKNOWN, "ff" * 28)]) == []


def test_identity_token_at_burn_credential_is_not_drift() -> None:
    deployment = default_deployment()
    market = _by_name()["Ada"]
    policies = [
        STRIKE_ADA_STATE_NFT,
        deployment.oracle_token_policy,
        *sorted(market.loan_token_policies),
    ]
    burned = [(BURN_CREDENTIAL, policy) for policy in policies]
    assert BURN_CREDENTIAL == "00" * 28
    assert check_deployment(observed_credentials=burned) == []
    # Any other unlisted key credential is still drift.
    findings = check_deployment(
        observed_credentials=[*burned, (UNKNOWN, STRIKE_ADA_STATE_NFT)],
    )
    assert [(f.kind, f.script_hash) for f in findings] == [("identity", UNKNOWN)]


def test_findings_are_sorted_and_deduplicated() -> None:
    rec, fields, _ = _live_layout36()
    fields[35] = bytes.fromhex(UNKNOWN)
    bad = record_info(rec, datum_cbor=cbor2.dumps(fields).hex())
    observed = [(UNKNOWN, STRIKE_ADA_STATE_NFT), ("cd" * 28, STRIKE_ADA_STATE_NFT)]
    registry = _bundled()
    registry["scriptInfos"].reverse()
    first = check_deployment(
        [bad, bad], registry=registry, observed_credentials=observed
    )
    second = check_deployment(
        [bad],
        registry=_bundled(),
        observed_credentials=list(reversed(observed * 2)),
    )
    assert first == second
    assert first == sorted(
        first,
        key=lambda f: (f.kind, f.market or "", f.component, f.script_hash, f.detail),
    )
    assert [f.kind for f in first] == ["identity", "identity", "params"]


def test_unknown_finding_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown drift kind"):
        DriftFinding("other", None, "market", "", "")


@pytest.mark.parametrize(
    "datum",
    ["c4821bffffffffffffff0101", "c5821bffffffffffffff0101"],
    ids=["decimal_fraction", "bigfloat"],
)
def test_overflowing_number_tag_params_datum_is_one_params_finding(
    datum: str,
) -> None:
    rec, _, market = _live_layout36()
    findings = check_deployment([record_info(rec, datum_cbor=datum)])
    assert _keys(findings) == [
        ("params", market.name, "datum", market.market_params_policy),
    ]


def test_params_drift_reads_the_shape_through_the_shared_field_count(
    monkeypatch,
) -> None:
    rec, fields, market = _live_layout36()
    seen = []

    def spy(datum):
        seen.append(datum)
        return 28

    monkeypatch.setattr(deployment, "field_count", spy)
    findings = check_deployment([record_info(rec)])
    assert seen == [rec["datum_cbor"]]
    assert _keys(findings) == [
        ("params", market.name, "layout", market.market_params_policy),
    ]


def test_an_arithmetic_error_while_decoding_params_is_one_params_finding(
    monkeypatch,
) -> None:
    rec, _, market = _live_layout36()

    def overflow(_datum):
        raise decimal.Overflow

    monkeypatch.setattr(deployment, "decode_market_params", overflow)
    assert _keys(check_deployment([record_info(rec)])) == [
        ("params", market.name, "datum", market.market_params_policy),
    ]


def test_resolve_deployment_defaults_to_the_bundled_snapshot() -> None:
    assert resolve_deployment() is default_deployment()
    assert resolve_deployment(None) is default_deployment()
    custom = load_deployment(_bundled())
    assert resolve_deployment(custom) is custom


def _loan_info() -> PoolStateInfo:
    return record_info(records_of("loan")[0])


def _spent_loan_key() -> LoanKey:
    key = lifecycle.loan_key(_loan_info())
    assert key is not None
    return key


@pytest.mark.parametrize(
    ("module", "call"),
    [
        ("indexing", lambda: indexing.liqwid_selectors()),
        ("deployment", lambda: check_deployment([])),
        ("lifecycle", lambda: lifecycle.loan_key(_loan_info())),
        (
            "lifecycle",
            lambda: lifecycle.classify_spend(
                _spent_loan_key(),
                None,
                lifecycle.loan_spend([], []),
            ),
        ),
        ("lifecycle", lambda: lifecycle.LiqwidLifecycle()),
        ("oracles", lambda: oracles.LiqwidOracleResolver().deployment),
        ("market", lambda: liqwid_market.LiqwidV2MarketSource()),
    ],
    ids=[
        "liqwid_selectors",
        "check_deployment",
        "loan_key",
        "classify_spend",
        "LiqwidLifecycle",
        "LiqwidOracleResolver",
        "LiqwidV2MarketSource",
    ],
)
def test_every_reader_resolves_a_missing_deployment_through_one_helper(
    monkeypatch,
    module,
    call,
) -> None:
    seen = []

    def spy(active=None):
        seen.append(active)
        return default_deployment()

    target = importlib.import_module(f"charli3_dendrite.lending.liqwid.{module}")
    assert target.resolve_deployment is resolve_deployment
    monkeypatch.setattr(target, "resolve_deployment", spy)
    call()
    assert None in seen
