"""Liqwid V2 loan lifecycle against captured mainnet spends and synthetic evidence."""

from __future__ import annotations

import subprocess
import sys
from collections import Counter

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.lifecycle import LiqwidLifecycle
from charli3_dendrite.lending.liqwid.lifecycle import LoanKey
from charli3_dendrite.lending.liqwid.lifecycle import LoanSpend
from charli3_dendrite.lending.liqwid.lifecycle import LoanTransition
from charli3_dendrite.lending.liqwid.lifecycle import classify_spend
from charli3_dendrite.lending.liqwid.lifecycle import collateral_units
from charli3_dendrite.lending.liqwid.lifecycle import loan_key
from charli3_dendrite.lending.liqwid.lifecycle import loan_spend
from charli3_dendrite.lending.liqwid.lifecycle import match_successors
from charli3_dendrite.lending.markets import PositionLifecycle
from charli3_dendrite.lending.units import script_credential
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.liqwid.records import spend_of

# Constr 0 [] in its definite and indefinite encodings: the admin parking datum.
EMPTY_DATUMS = {"d87980", "d8799fff"}

T = LoanTransition

# rule key -> (tx hash prefix or None, transition of every spent loan of that tx)
EXPECTED = {
    "legacy_liquidation": (None, T.PARTIALLY_LIQUIDATED),
    "legacy_repay": (None, T.REPAID),
    "legacy_flip": (None, T.MODIFIED),
    "admin_migration": ("79e2bda3", T.MIGRATED),
    "admin_reown": ("c3115839", T.MIGRATED),
    "same_generation_rewrite": ("3a50a14b", T.MIGRATED),
    "parking": (None, T.PARKED),
    "new_modification": ("852f9f75", T.MODIFIED),
    "partial_liquidation": ("17a4aabe", T.PARTIALLY_LIQUIDATED),
    "full_repay": ("b114f920", T.REPAID),
}
ALL_SPENDS = [*EXPECTED, "multi_loan"]

OWNER_A = "aa" * 28
OWNER_B = "bb" * 28
QTOKEN = "cc" * 28
UNKNOWN_SCRIPT = "00" * 28


def _keys(records: list[dict]) -> list[LoanKey]:
    keys = [loan_key(record_info(rec)) for rec in records]
    assert all(key is not None for key in keys)
    return [key for key in keys if key is not None]


def _evidence(spend: dict) -> LoanSpend:
    return loan_spend(
        spend["withdrawals"],
        [(policy, quantity) for policy, quantity in spend["mints"]],
    )


def _transitions(spend: dict) -> list[tuple[LoanKey, LoanKey | None, LoanTransition]]:
    spent = _keys(spend["spent"])
    created = _keys(spend["created"])
    links = match_successors(spent, created)
    by_ref = {key.out_ref: key for key in created}
    evidence = _evidence(spend)
    out = []
    for key in spent:
        successor = by_ref.get(links.get(key.out_ref, ""))
        out.append((key, successor, classify_spend(key, successor, evidence)))
    return out


def _key(
    out_ref: str,
    *,
    market_id: str = "m1",
    owner: str | None = OWNER_A,
    principal: int = 100,
    collateral: tuple[tuple[str, int], ...] = ((QTOKEN, 5),),
    generation: str = "new",
) -> LoanKey:
    return LoanKey(
        out_ref=out_ref,
        market_id=market_id,
        owner=owner,
        principal=principal,
        interest=0,
        min_interest=0,
        loan_index=1,
        collateral=collateral,
        generation=generation,
    )


def _full_market():
    """A market with both staking validators, a Liquidation and a Borrow policy."""
    return next(
        m
        for m in default_deployment().markets
        if m.liquidation_staking
        and m.borrow_staking
        and m.liquidation_policy
        and m.borrow_policy
    )


def _genuine_loan() -> dict:
    """The first captured loan record that ``parse_utxo`` accepts as a loan."""
    for rec in records_of("loan"):
        record = parse_utxo(record_info(rec))
        if record is not None and record.kind is LiqwidKind.LOAN:
            return rec
    raise AssertionError("no genuine loan record captured")


# --- captured mainnet spends -------------------------------------------------------


@pytest.mark.parametrize("rule", list(EXPECTED))
def test_captured_spend_classifies_as_expected(rule):
    prefix, expected = EXPECTED[rule]
    spend = spend_of(rule)
    if prefix is not None:
        assert spend["tx_hash"].startswith(prefix)
    results = _transitions(spend)
    assert results
    assert [transition for _, _, transition in results] == [expected] * len(results)


@pytest.mark.parametrize("rule", ALL_SPENDS)
def test_every_captured_spend_classifies_to_a_member(rule):
    for _, _, transition in _transitions(spend_of(rule)):
        assert isinstance(transition, LoanTransition)


def test_admin_reown_moves_the_loan_to_the_new_owner():
    spend = spend_of("admin_reown")
    created = {rec["out_ref"]: rec for rec in spend["created"]}
    results = _transitions(spend)
    assert results
    for spent, successor, transition in results:
        assert transition is T.MIGRATED
        assert successor is not None
        assert successor.owner is not None
        assert successor.owner != spent.owner
        datum = decode_loan(created[successor.out_ref]["datum_cbor"])
        assert successor.owner == datum.owner


def test_same_generation_rewrite_keeps_owner_and_generation():
    spend = spend_of("same_generation_rewrite")
    results = _transitions(spend)
    assert results
    for spent, successor, transition in results:
        assert transition is T.MIGRATED
        assert successor is not None
        assert (successor.owner, successor.generation) == (
            spent.owner,
            spent.generation,
        )
    evidence = _evidence(spend)
    assert not evidence.burned_any
    assert all(quantity == 0 for quantity in evidence.minted.values())


def test_parking_output_has_no_owner_and_ends_the_chain():
    spend = spend_of("parking")
    parked = [rec for rec in spend["created"] if rec["datum_cbor"] in EMPTY_DATUMS]
    assert parked
    for rec in parked:
        key = loan_key(record_info(rec))
        assert key is not None
        assert key.owner is None
    for _, successor, transition in _transitions(spend):
        assert successor is None
        assert transition is T.PARKED


def test_multi_loan_same_owner_resolves_by_state():
    spend = spend_of("multi_loan")
    spent = _keys(spend["spent"])
    created = _keys(spend["created"])
    owners = Counter(key.owner for key in spent)
    shared = {owner for owner, count in owners.items() if count > 1}
    assert shared
    links = match_successors(spent, created)
    by_ref = {key.out_ref: key for key in created}
    state_linked = [key for key in spent if key.owner in shared]
    assert all(key.out_ref in links for key in state_linked)
    for key in state_linked:
        successor = by_ref[links[key.out_ref]]
        assert (
            key.principal,
            key.interest,
            key.min_interest,
            key.loan_index,
            key.collateral,
        ) == (
            successor.principal,
            successor.interest,
            successor.min_interest,
            successor.loan_index,
            successor.collateral,
        )


@pytest.mark.parametrize("rule", ALL_SPENDS)
def test_captured_spend_matches_its_recorded_successors(rule):
    spend = spend_of(rule)
    expected = {
        out_ref: (entry["successor"], LoanTransition(entry["transition"]))
        for out_ref, entry in spend["expected"].items()
    }
    assert expected
    actual = {
        key.out_ref: (
            successor.out_ref if successor is not None else None,
            transition,
        )
        for key, successor, transition in _transitions(spend)
    }
    assert actual == expected


# --- loan_key ----------------------------------------------------------------------


def test_loan_key_of_a_genuine_loan():
    rec = _genuine_loan()
    info = record_info(rec)
    key = loan_key(info)
    datum = decode_loan(rec["datum_cbor"])
    deployment = default_deployment()
    market, generation = deployment.market_of_loan_validator(
        script_credential(rec["address"]),
    )
    assert key == LoanKey(
        out_ref=rec["out_ref"],
        market_id=market.market_id,
        owner=datum.owner,
        principal=datum.principal,
        interest=datum.interest,
        min_interest=datum.min_interest,
        loan_index=datum.index,
        collateral=key.collateral,
        generation=generation,
    )
    units = [unit for unit, _ in key.collateral]
    assert units == sorted(units)
    assert "lovelace" not in units
    assert not {unit[:56] for unit in units} & deployment.loan_token_policies
    assert all(quantity > 0 for _, quantity in key.collateral)


def test_loan_key_of_a_datumless_output_has_no_owner():
    dust = [rec for rec in records_of("loan") if not rec["datum_cbor"]]
    assert dust
    key = loan_key(record_info(dust[0]))
    assert key is not None
    assert key.owner is None
    assert (key.principal, key.interest, key.min_interest, key.loan_index) == (
        0,
        0,
        0,
        0,
    )


def test_loan_key_of_a_stripped_loan_keeps_market_and_generation():
    rec = _genuine_loan()
    full = loan_key(record_info(rec))
    stripped = loan_key(record_info(rec, datum_cbor=""))
    assert stripped is not None
    assert full is not None
    assert stripped.owner is None
    assert (stripped.market_id, stripped.generation, stripped.collateral) == (
        full.market_id,
        full.generation,
        full.collateral,
    )
    for mangled in (rec["datum_cbor"][:-2], "zz" + rec["datum_cbor"][2:], "ff"):
        broken = loan_key(record_info(rec, datum_cbor=mangled))
        assert broken is not None, mangled
        assert broken.owner is None
        assert broken.principal == 0
        assert (broken.market_id, broken.generation) == (
            full.market_id,
            full.generation,
        )


def test_loan_key_is_none_off_the_loan_validators():
    assert loan_key(record_info(records_of("market_params")[0])) is None
    rec = _genuine_loan()
    assert (
        loan_key(
            record_info(rec, address=script_payment_address(UNKNOWN_SCRIPT)),
        )
        is None
    )
    assert loan_key(record_info(rec, address="not-an-address")) is None


# --- match_successors --------------------------------------------------------------


def test_pass_one_links_unique_owners():
    spent = [_key("s#0", owner=OWNER_A), _key("s#1", owner=OWNER_B)]
    created = [
        _key("c#0", owner=OWNER_B, principal=7),
        _key("c#1", owner=OWNER_A, principal=9),
    ]
    assert match_successors(spent, created) == {"s#0": "c#1", "s#1": "c#0"}


def test_shared_owner_falls_through_to_the_state_pass():
    spent = [_key("s#0", principal=10), _key("s#1", principal=20)]
    created = [_key("c#0", principal=20), _key("c#1", principal=10)]
    assert match_successors(spent, created) == {"s#0": "c#1", "s#1": "c#0"}


def test_state_pass_links_a_reowned_loan():
    spent = [_key("s#0", owner=OWNER_A)]
    created = [_key("c#0", owner=OWNER_B)]
    assert match_successors(spent, created) == {"s#0": "c#0"}


def test_ambiguous_owner_and_state_stay_unmatched():
    spent = [_key("s#0"), _key("s#1")]
    created = [_key("c#0"), _key("c#1")]
    assert match_successors(spent, created) == {}


def test_state_pass_requires_uniqueness_on_both_sides():
    spent = [_key("s#0", principal=10), _key("s#1", principal=20)]
    created = [_key("c#0", principal=10), _key("c#1", principal=10)]
    assert match_successors(spent, created) == {}


def test_state_pass_compares_collateral():
    spent = [_key("s#0", collateral=((QTOKEN, 5),))]
    created = [_key("c#0", owner=OWNER_B, collateral=((QTOKEN, 6),))]
    assert match_successors(spent, created) == {}


def test_matching_never_crosses_markets():
    spent = [_key("s#0", market_id="m1")]
    created = [_key("c#0", market_id="m2")]
    assert match_successors(spent, created) == {}
    spent = [_key("s#0", market_id="m1"), _key("s#1", market_id="m2")]
    created = [_key("c#0", market_id="m2"), _key("c#1", market_id="m1")]
    assert match_successors(spent, created) == {"s#0": "c#1", "s#1": "c#0"}


def test_ownerless_keys_are_never_linked():
    parked = _key("c#0", owner=None, principal=0, collateral=())
    assert match_successors([_key("s#0")], [parked]) == {}
    unparked = _key("s#0", owner=None, principal=0, collateral=())
    same_state = _key("c#0", owner=None, principal=0, collateral=())
    assert match_successors([unparked], [same_state]) == {}


def test_matching_with_nothing_on_either_side():
    assert match_successors([], []) == {}
    assert match_successors([_key("s#0")], []) == {}
    assert match_successors([], [_key("c#0")]) == {}


# --- loan_spend --------------------------------------------------------------------


def test_loan_spend_nets_a_flip_to_zero_and_lists_the_burn():
    policy = "dd" * 28
    spend = loan_spend([], [(policy, -1), (policy, 1)])
    assert dict(spend.minted) == {policy: 0}
    assert spend.burned_any == frozenset({policy})
    assert spend.withdrawal_scripts == frozenset()


def test_loan_spend_counts_unit_rows_by_policy():
    policy = "dd" * 28
    other = "ee" * 28
    spend = loan_spend(
        ["ff" * 28, "ff" * 28],
        [(policy + "00", -1), (policy + "01", 1), (other, 2), (other + "01", 3)],
    )
    assert dict(spend.minted) == {policy: 0, other: 5}
    assert spend.burned_any == frozenset({policy})
    assert spend.withdrawal_scripts == frozenset({"ff" * 28})


def test_loan_spend_without_rows_is_empty():
    spend = loan_spend([], [])
    assert spend == LoanSpend(
        withdrawal_scripts=frozenset(),
        minted={},
        burned_any=frozenset(),
    )


# --- classify_spend ----------------------------------------------------------------

EVIDENCE = {
    "liquidation_withdrawal": lambda m: loan_spend([m.liquidation_staking[0]], []),
    "borrow_withdrawal": lambda m: loan_spend([m.borrow_staking[0]], []),
    "liquidation_mint": lambda m: loan_spend([], [(m.liquidation_policy, 1)]),
    "loan_burn": lambda m: loan_spend([], [(m.borrow_policy + "00", -1)]),
    "loan_flip": lambda m: loan_spend(
        [],
        [(m.borrow_policy + "00", -1), (m.borrow_policy + "01", 1)],
    ),
    "none": lambda m: loan_spend([], []),
}


@pytest.mark.parametrize(
    ("evidence", "continued", "expected"),
    [
        ("liquidation_withdrawal", True, T.PARTIALLY_LIQUIDATED),  # rule 1
        ("liquidation_withdrawal", False, T.LIQUIDATED),  # rule 1
        ("borrow_withdrawal", True, T.MODIFIED),  # rule 2
        ("borrow_withdrawal", False, T.REPAID),  # rule 2
        ("liquidation_mint", True, T.PARTIALLY_LIQUIDATED),  # rule 3
        ("liquidation_mint", False, T.LIQUIDATED),  # rule 3
        ("loan_burn", False, T.REPAID),  # rule 4
        ("loan_flip", True, T.MODIFIED),  # rule 5
        ("none", False, T.PARKED),  # rule 7
        ("none", True, T.MIGRATED),  # rule 8
    ],
)
def test_classify_spend_rules(evidence, continued, expected):
    market = _full_market()
    spent = _key("s#0", market_id=market.market_id)
    successor = _key("c#0", market_id=market.market_id) if continued else None
    spend = EVIDENCE[evidence](market)
    assert classify_spend(spent, successor, spend) is expected


def test_flip_beside_a_repay_of_the_same_policy_migrates():
    # Evidence is per transaction: repaying one loan (burn 00) and flipping another
    # (burn 00, mint 01) nets the policy to -1, so the flipped loan fails rule 5.
    market = _full_market()
    spent = _key("s#0", market_id=market.market_id)
    successor = _key("c#0", market_id=market.market_id)
    spend = loan_spend(
        [],
        [
            (market.borrow_policy + "00", -1),
            (market.borrow_policy + "00", -1),
            (market.borrow_policy + "01", 1),
        ],
    )
    assert dict(spend.minted) == {market.borrow_policy: -1}
    assert classify_spend(spent, successor, spend) is T.MIGRATED
    assert classify_spend(spent, None, spend) is T.REPAID


@pytest.mark.parametrize(
    "successor_overrides",
    [{"generation": "legacy"}, {"owner": OWNER_B}],
)
def test_successor_elsewhere_without_evidence_migrates(successor_overrides):
    market = _full_market()
    spent = _key("s#0", market_id=market.market_id, generation="new")
    successor = _key("c#0", market_id=market.market_id, **successor_overrides)
    assert classify_spend(spent, successor, loan_spend([], [])) is T.MIGRATED


def test_liquidation_withdrawal_outranks_borrow_withdrawal():
    market = _full_market()
    spent = _key("s#0", market_id=market.market_id)
    spend = loan_spend([market.borrow_staking[0], market.liquidation_staking[0]], [])
    assert classify_spend(spent, None, spend) is T.LIQUIDATED


def test_borrow_withdrawal_outranks_liquidation_mint():
    market = _full_market()
    spent = _key("s#0", market_id=market.market_id)
    successor = _key("c#0", market_id=market.market_id)
    spend = loan_spend([market.borrow_staking[0]], [(market.liquidation_policy, 1)])
    assert classify_spend(spent, successor, spend) is T.MODIFIED


def test_another_markets_staking_is_not_evidence():
    market = _full_market()
    other = next(
        m
        for m in default_deployment().markets
        if m.market_id != market.market_id
        and m.borrow_staking
        and not set(m.borrow_staking) & set(market.borrow_staking)
    )
    spent = _key("s#0", market_id=market.market_id)
    spend = loan_spend([other.borrow_staking[0]], [])
    assert classify_spend(spent, None, spend) is T.PARKED


def test_unknown_market_still_classifies():
    market = _full_market()
    spent = _key("s#0", market_id="not-a-market")
    spend = loan_spend([market.liquidation_staking[0]], [(market.borrow_policy, -1)])
    assert classify_spend(spent, None, spend) is T.PARKED
    successor = _key("c#0", market_id="not-a-market")
    assert classify_spend(spent, successor, spend) is T.MIGRATED


@pytest.mark.parametrize("continued", [True, False])
def test_empty_evidence_returns_a_member(continued):
    for market in default_deployment().markets:
        spent = _key("s#0", market_id=market.market_id)
        successor = _key("c#0", market_id=market.market_id) if continued else None
        transition = classify_spend(spent, successor, loan_spend([], []))
        assert isinstance(transition, LoanTransition)
        assert transition is (T.MIGRATED if continued else T.PARKED)


# --- LiqwidLifecycle ---------------------------------------------------------------


def test_lifecycle_satisfies_the_protocol():
    lifecycle = LiqwidLifecycle()
    assert isinstance(lifecycle, PositionLifecycle)
    assert lifecycle.loan_token_policies == default_deployment().loan_token_policies
    assert lifecycle.loan_token_policies


def test_lifecycle_methods_match_the_functions():
    deployment = default_deployment()
    lifecycle = LiqwidLifecycle(deployment)
    for rule in ALL_SPENDS:
        spend = spend_of(rule)
        for rec in spend["spent"] + spend["created"]:
            info = record_info(rec)
            assert lifecycle.loan_key(info) == loan_key(info, deployment)
        spent = _keys(spend["spent"])
        created = _keys(spend["created"])
        links = lifecycle.match_successors(spent, created)
        assert links == match_successors(spent, created)
        by_ref = {key.out_ref: key for key in created}
        evidence = _evidence(spend)
        for key in spent:
            successor = by_ref.get(links.get(key.out_ref, ""))
            assert lifecycle.classify_spend(key, successor, evidence) is (
                classify_spend(key, successor, evidence, deployment)
            )


@pytest.mark.parametrize(
    "modules",
    [
        (
            "charli3_dendrite.lending.markets",
            "charli3_dendrite.lending.liqwid.lifecycle",
        ),
        (
            "charli3_dendrite.lending.liqwid.lifecycle",
            "charli3_dendrite.lending.markets",
        ),
    ],
)
def test_lifecycle_imports_in_either_order(modules):
    code = "; ".join(f"import {name}" for name in modules)
    subprocess.run([sys.executable, "-c", code], check=True)


def test_collateral_units_drop_lovelace_and_loan_tokens_sorted_by_unit():
    deployment = default_deployment()
    market = deployment.markets[0]
    rec = _genuine_loan()
    assets = Assets(
        root={
            "lovelace": 2_000_000,
            "ff" * 28 + "01": 3,
            market.borrow_policy + "00": 1,
            "aa" * 28 + "02": 9,
            "aa" * 28: 7,
        },
    )
    info = record_info(rec, assets=assets)
    assert collateral_units(info, deployment.loan_token_policies) == (
        ("aa" * 28, 7),
        ("aa" * 28 + "02", 9),
        ("ff" * 28 + "01", 3),
    )
    assert collateral_units(info, frozenset()) == (
        ("aa" * 28, 7),
        ("aa" * 28 + "02", 9),
        (market.borrow_policy + "00", 1),
        ("ff" * 28 + "01", 3),
    )


def test_loan_key_collateral_is_the_shared_collateral_units():
    deployment = default_deployment()
    for rec in records_of("loan"):
        info = record_info(rec)
        key = loan_key(info, deployment)
        if key is None:
            continue
        assert key.collateral == collateral_units(
            info,
            deployment.loan_token_policies,
        )
