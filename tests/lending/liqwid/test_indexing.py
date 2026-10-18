"""Selecting and classifying Liqwid V2 UTxO records (offline, captured mainnet)."""

import dataclasses
from collections import Counter

import cbor2
import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import OracleFeedDatum
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
from charli3_dendrite.lending.liqwid.indexing import LiqwidSelector
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.markets import CONTEXT_ROLE
from charli3_dendrite.lending.markets import VIEW_ROLE
from charli3_dendrite.lending.markets import WatchSpec
from charli3_dendrite.lending.units import plutus_fields
from charli3_dendrite.lending.units import script_credential
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of

DATUM_CLASS = {
    LiqwidKind.MARKET_STATE: MarketStateDatum,
    LiqwidKind.LOAN: LoanDatum,
    LiqwidKind.MARKET_PARAMS: MarketParamsDatum,
    LiqwidKind.COLLATERAL_PARAMS: CollateralParamsDatum,
    LiqwidKind.ORACLE: OracleFeedDatum,
}

# Captured non-records. Each sits at a watched credential, so only its identity token
# or its datum shape tells it apart from a record.
REJECTED_LABELS = (
    "oracle_decoy",  # token-less, well-formed feed datum at the hard-caps oracle
    "param_dust",  # ADA-only UTxO at the parameter address
    "dead_policy_collateral_params",  # CollateralParams NFT under a retired policy
    "parked_market_params",  # C0[] output holding a MarketParams NFT
    "parking_loan",  # C0[] output holding a loan token at a loan validator
    "datumless_loan",  # dust UTxO with no inline datum at a loan validator
    "state_list0",  # empty list holding the Ada MarketState NFT
    "state_list27",  # 27-field list holding the Ada MarketState NFT
)

# Ways to break a genuine record's datum while keeping its credential and token.
MANGLES = {
    "truncated_by_one_byte": lambda datum: datum[:-2],
    "truncated_by_half": lambda datum: datum[: len(datum) // 4 * 2],
    "not_hex": lambda datum: "zz" + datum[2:],
    "lone_break_byte": lambda datum: "ff",
    "reserved_header": lambda datum: "1c",
    "nested_past_the_recursion_limit": lambda datum: "81" * 100_000 + "00",
    "decimal_fraction_exponent_overflow": lambda datum: "c4821bffffffffffffff0101",
    "bigfloat_exponent_overflow": lambda datum: "c5821bffffffffffffff0101",
}


def _identity_policy(rec, kind):
    """The token policy a genuine record of ``kind`` holds, from the deployment."""
    deployment = default_deployment()
    if kind is LiqwidKind.ORACLE:
        return deployment.oracle_token_policy
    market = deployment.market(rec["market_id"])
    if kind is LiqwidKind.LOAN:
        if rec["generation"] == "new":
            return market.new_borrow_policy
        return market.borrow_policy
    return {
        LiqwidKind.MARKET_STATE: market.market_state_policy,
        LiqwidKind.MARKET_PARAMS: market.market_params_policy,
        LiqwidKind.COLLATERAL_PARAMS: market.collateral_params_policy,
    }[kind]


def _with_assets(rec, assets):
    return record_info({**rec, "assets": assets})


def _with_field(rec, index, value):
    """``rec``'s datum re-encoded with field ``index`` replaced by ``value``."""
    fields = plutus_fields(cbor2.loads(bytes.fromhex(rec["datum_cbor"])))
    fields[index] = value
    return cbor2.dumps(fields).hex()


def _rejects(label):
    recs = [rec for rec in records_of("reject") if rec["label"] == label]
    assert recs, label
    return recs


def _genuine(kind, *, live=None):
    """The genuine records of ``kind``; its list also holds labelled non-records."""
    recs = [rec for rec in records_of(kind, live=live) if rec["label"] is None]
    assert recs, kind
    return recs


def test_kind_values():
    assert [kind.value for kind in LiqwidKind] == [
        "market_state",
        "loan",
        "market_params",
        "collateral_params",
        "oracle",
    ]


@pytest.mark.parametrize("kind", list(LiqwidKind), ids=lambda k: k.value)
def test_every_captured_record_is_accepted(kind):
    recs = _genuine(kind.value)
    assert recs
    for rec in recs:
        record = parse_utxo(record_info(rec))
        assert record is not None, rec["out_ref"]
        assert record.kind is kind, rec["out_ref"]
        assert record.out_ref == rec["out_ref"]
        assert record.market_id == rec["market_id"], rec["out_ref"]
        assert record.generation == rec["generation"], rec["out_ref"]
        assert isinstance(record.datum, DATUM_CLASS[kind]), rec["out_ref"]


@pytest.mark.parametrize("label", REJECTED_LABELS)
def test_captured_non_records_are_rejected(label):
    for rec in _rejects(label):
        assert parse_utxo(record_info(rec)) is None, rec["out_ref"]


def test_all_four_oracle_decoys_are_captured():
    assert len(_rejects("oracle_decoy")) == 4


def test_a_market_state_is_accepted_at_every_state_validator_of_its_market():
    deployment = default_deployment()
    for rec in _genuine("market_state", live=True):
        market = deployment.market(rec["market_id"])
        for validator in market.market_state_validators:
            info = record_info(rec, address=script_payment_address(validator))
            record = parse_utxo(info)
            assert record is not None, (rec["out_ref"], validator)
            assert record.market_id == rec["market_id"]


def test_a_market_state_at_another_markets_validator_is_rejected():
    deployment = default_deployment()
    rec = _genuine("market_state", live=True)[0]
    other = next(m for m in deployment.markets if m.market_id != rec["market_id"])
    address = script_payment_address(other.market_state_validators[0])
    assert parse_utxo(record_info(rec, address=address)) is None


@pytest.mark.parametrize("kind", list(LiqwidKind), ids=lambda k: k.value)
def test_a_wrong_credential_is_rejected(kind):
    rec = _genuine(kind.value, live=True)[0]
    credential = script_credential(rec["address"])
    assert credential is not None
    key_address = Address(
        payment_part=VerificationKeyHash(bytes.fromhex(credential)),
        network=Network.MAINNET,
    ).encode()
    assert parse_utxo(record_info(rec, address=key_address)) is None
    other_script = script_payment_address("00" * 28)
    assert parse_utxo(record_info(rec, address=other_script)) is None


@pytest.mark.parametrize("kind", list(LiqwidKind), ids=lambda k: k.value)
def test_the_identity_token_must_be_exactly_one(kind):
    rec = _genuine(kind.value, live=True)[0]
    policy = _identity_policy(rec, kind)
    held = [unit for unit in rec["assets"] if unit.startswith(policy)]
    assert len(held) == 1
    unit = held[0]
    others = {u: q for u, q in rec["assets"].items() if u != unit}
    second = policy + ("02" if unit != policy + "02" else "03")
    assert parse_utxo(_with_assets(rec, {**others, unit: 1})) is not None
    assert parse_utxo(_with_assets(rec, others)) is None
    assert parse_utxo(_with_assets(rec, {**others, unit: 2})) is None
    assert parse_utxo(_with_assets(rec, {**others, unit: 1, second: 1})) is None


@pytest.mark.parametrize(
    "kind",
    [kind for kind in LiqwidKind if kind is not LiqwidKind.LOAN],
    ids=lambda k: k.value,
)
def test_market_and_oracle_tokens_must_have_an_empty_name(kind):
    rec = _genuine(kind.value, live=True)[0]
    policy = _identity_policy(rec, kind)
    assets = {u: q for u, q in rec["assets"].items() if u != policy}
    assets[policy + "01"] = 1
    assert parse_utxo(_with_assets(rec, assets)) is None


@pytest.mark.parametrize("kind", list(LiqwidKind), ids=lambda k: k.value)
def test_a_record_without_an_inline_datum_is_rejected(kind):
    rec = _genuine(kind.value, live=True)[0]
    assert parse_utxo(record_info({**rec, "datum_cbor": ""})) is None


@pytest.mark.parametrize(("name", "mangle"), MANGLES.items(), ids=list(MANGLES))
@pytest.mark.parametrize("kind", list(LiqwidKind), ids=lambda k: k.value)
def test_a_malformed_datum_returns_none_without_raising(kind, name, mangle):
    rec = _genuine(kind.value, live=True)[0]
    assert parse_utxo(record_info(rec)) is not None
    datum = mangle(rec["datum_cbor"])
    assert parse_utxo(record_info({**rec, "datum_cbor": datum})) is None, name


@pytest.mark.parametrize(
    ("kind", "index"),
    [(LiqwidKind.LOAN, 1), (LiqwidKind.MARKET_STATE, 0)],
    ids=["loan_principal", "market_state_cash"],
)
def test_a_wrong_typed_field_returns_none_without_raising(kind, index):
    rec = _genuine(kind.value, live=True)[0]
    datum = _with_field(rec, index, b"\x01")
    fields = plutus_fields(cbor2.loads(bytes.fromhex(datum)))
    assert len(fields) == len(
        plutus_fields(cbor2.loads(bytes.fromhex(rec["datum_cbor"]))),
    )
    assert parse_utxo(record_info({**rec, "datum_cbor": datum})) is None


def test_selector_counts_per_kind_and_role():
    deployment = default_deployment()
    counts = Counter((s.kind, s.role) for s in liqwid_selectors())
    # 41 MarketState validators (35 registry, 2 audit, 4 launch), 69 loan
    # validators, 35 markets, 3 oracle validators (V1, hard caps, pegs).
    assert counts == {
        (LiqwidKind.MARKET_STATE, VIEW_ROLE): 41,
        (LiqwidKind.LOAN, VIEW_ROLE): 69,
        (LiqwidKind.MARKET_PARAMS, CONTEXT_ROLE): 35,
        (LiqwidKind.COLLATERAL_PARAMS, CONTEXT_ROLE): 35,
        (LiqwidKind.ORACLE, CONTEXT_ROLE): 3,
    }
    markets = deployment.markets
    assert counts[(LiqwidKind.MARKET_STATE, VIEW_ROLE)] == sum(
        len(m.market_state_validators) for m in markets
    )
    assert counts[(LiqwidKind.LOAN, VIEW_ROLE)] == sum(
        len(m.loan_validators) for m in markets
    )
    assert counts[(LiqwidKind.MARKET_PARAMS, CONTEXT_ROLE)] == len(markets)
    assert counts[(LiqwidKind.ORACLE, CONTEXT_ROLE)] == len(
        deployment.oracle_validators,
    )


def test_views_come_first_then_context():
    roles = [s.role for s in liqwid_selectors()]
    first_context = roles.index(CONTEXT_ROLE)
    assert set(roles[:first_context]) == {VIEW_ROLE}
    assert set(roles[first_context:]) == {CONTEXT_ROLE}


def test_selectors_are_deterministic_and_unique():
    selectors = liqwid_selectors()
    assert selectors == liqwid_selectors()
    assert selectors == liqwid_selectors(default_deployment())
    assert len(set(selectors)) == len(selectors)
    assert all(isinstance(s, LiqwidSelector) for s in selectors)


def test_view_selectors_pair_each_validator_with_its_market_token():
    deployment = default_deployment()
    selectors = liqwid_selectors()
    states = {
        (s.payment_credential, s.identity_policy, s.market_id)
        for s in selectors
        if s.kind is LiqwidKind.MARKET_STATE
    }
    assert states == {
        (validator, m.market_state_policy, m.market_id)
        for m in deployment.markets
        for validator in m.market_state_validators
    }
    loans = {
        (s.payment_credential, s.identity_policy, s.market_id)
        for s in selectors
        if s.kind is LiqwidKind.LOAN
    }
    assert loans == {
        (validator, m.borrow_policy, m.market_id)
        for m in deployment.markets
        for validator in m.legacy_loan_validators
    } | {
        (validator, m.new_borrow_policy, m.market_id)
        for m in deployment.markets
        for validator in m.new_loan_validators
    }


def test_context_selectors_cover_params_and_oracles():
    deployment = default_deployment()
    selectors = liqwid_selectors()

    def of(kind):
        return [s for s in selectors if s.kind is kind]

    params = of(LiqwidKind.MARKET_PARAMS)
    assert [s.identity_policy for s in params] == [
        m.market_params_policy for m in deployment.markets
    ]
    collateral = of(LiqwidKind.COLLATERAL_PARAMS)
    assert [s.identity_policy for s in collateral] == [
        m.collateral_params_policy for m in deployment.markets
    ]
    for selector in params + collateral:
        assert selector.payment_credential == deployment.param_credential
    oracles = of(LiqwidKind.ORACLE)
    assert [s.payment_credential for s in oracles] == [
        o.script_hash for o in deployment.oracle_validators
    ]
    assert {s.identity_policy for s in oracles} == {deployment.oracle_token_policy}
    assert {s.market_id for s in oracles} == {None}


def test_action_and_batch_validators_are_not_selectors():
    deployment = default_deployment()
    batch_side = {
        script
        for m in deployment.markets
        for script in (*m.actions, *m.batches, *m.batch_finals)
    }
    assert batch_side
    credentials = {s.payment_credential for s in liqwid_selectors()}
    assert credentials.isdisjoint(batch_side)


def test_selector_address_and_watch_spec():
    for selector in liqwid_selectors():
        assert selector.address == script_payment_address(selector.payment_credential)
        assert selector.watch_spec() == WatchSpec(
            payment_credential=selector.payment_credential,
            identity_policy=selector.identity_policy,
            role=selector.role,
        )


def test_record_equality_ignores_the_utxo_info():
    rec = _genuine("loan", live=True)[0]
    first = parse_utxo(record_info(rec))
    second = parse_utxo(record_info(rec, block_time=1))
    assert isinstance(first, LiqwidRecord)
    assert first.info.block_time != second.info.block_time
    assert first == second
    assert hash(first) == hash(second)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.kind = LiqwidKind.ORACLE


def test_parse_follows_the_given_deployment():
    deployment = default_deployment()
    rec = _genuine("market_state", live=True)[0]
    assert parse_utxo(record_info(rec), deployment) == parse_utxo(record_info(rec))
    without = dataclasses.replace(
        deployment,
        markets=tuple(m for m in deployment.markets if m.market_id != rec["market_id"]),
    )
    assert parse_utxo(record_info(rec), without) is None
    assert all(s.market_id != rec["market_id"] for s in liqwid_selectors(without))
