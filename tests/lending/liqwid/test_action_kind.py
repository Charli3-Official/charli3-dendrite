"""The Liqwid V2 action UTxO kind, kept outside the read-side routes (offline)."""

import dataclasses
from collections import Counter

import cbor2
import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.liqwid import indexing
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.datums import decode_market_params
from charli3_dendrite.lending.liqwid.datums import encode_action_datum
from charli3_dendrite.lending.liqwid.deployment import NEW_GENERATION
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
from charli3_dendrite.lending.liqwid.indexing import LiqwidSelector
from charli3_dendrite.lending.liqwid.indexing import action_selector
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import param_selector
from charli3_dendrite.lending.liqwid.indexing import parse_action
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.indexing import state_selectors
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.markets import CONTEXT_ROLE
from charli3_dendrite.lending.markets import VIEW_ROLE
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.liqwid.records import UTXO_KINDS
from tests.lending.liqwid.records import records_of

TX_HASH = "ab" * 32
ACTION = ActionDatum(
    supply_delta=5_000_000,
    qtoken_delta=-250_000_000,
    principal_delta=1_000_000,
    interest_delta=0,
    min_interest_paid=800,
    nonce=cbor2.CBORTag(121, [b"\x07"]),
)


def _live_params():
    """Each market with its live MarketParams datum, in capture order."""
    deployment = default_deployment()
    return [
        (deployment.market(rec["market_id"]), decode_market_params(rec["datum_cbor"]))
        for rec in records_of("market_params", live=True)
        if rec["label"] is None
    ]


def _market_and_params(name="Ada"):
    return next((m, p) for m, p in _live_params() if m.name == name)


def _action_info(market, params, *, assets=None, datum=None, tx_index=3):
    """A synthetic action UTxO at ``params``' action validator."""
    policy = market.action_token_policy
    if assets is None:
        assets = {"lovelace": 2_000_000, policy: 1}
    if datum is None:
        datum = encode_action_datum(ACTION).hex()
    return PoolStateInfo(
        address=script_payment_address(params.action_validator),
        tx_hash=TX_HASH,
        tx_index=tx_index,
        block_time=1_790_000_000,
        block_index=0,
        block_hash="",
        datum_hash="",
        datum_cbor=datum,
        assets=Assets(root=dict(assets)),
        plutus_v2=False,
    )


def test_action_kind_value_and_decoder():
    assert LiqwidKind.ACTION.value == "action"
    assert LiqwidKind("action") is LiqwidKind.ACTION
    assert indexing._DECODERS[LiqwidKind.ACTION] is decode_action


def test_the_read_side_selectors_are_unchanged():
    selectors = liqwid_selectors()
    assert len(selectors) == 41 + 69 + 35 + 35 + 3
    assert LiqwidKind.ACTION not in {s.kind for s in selectors}
    assert {s.kind.value for s in selectors} == set(UTXO_KINDS)


def test_action_selector_reads_the_live_action_validator():
    pairs = _live_params()
    assert len(pairs) == len(default_deployment().markets)
    for market, params in pairs:
        selector = action_selector(market, params)
        assert isinstance(selector, LiqwidSelector)
        assert selector.kind is LiqwidKind.ACTION
        assert selector.payment_credential == params.action_validator
        assert selector.payment_credential in market.actions
        assert selector.identity_policy == market.action_token_policy
        assert selector.market_id == market.market_id
        assert selector.role == CONTEXT_ROLE
        assert selector.address == script_payment_address(params.action_validator)


def test_action_selector_refuses_a_market_without_an_action_script():
    market, params = _market_and_params()
    no_validator = dataclasses.replace(params, action_validator=None)
    with pytest.raises(UnsupportedMarketError) as info:
        action_selector(market, no_validator)
    assert info.value.market == market.name
    assert "action validator" in info.value.reason
    no_policy = dataclasses.replace(market, action_token_policy=None)
    with pytest.raises(UnsupportedMarketError) as info:
        action_selector(no_policy, params)
    assert info.value.market == market.name
    assert "action-token policy" in info.value.reason


@pytest.mark.parametrize("indefinite", [False, True], ids=["definite", "indefinite"])
def test_parse_action_accepts_a_genuine_action_utxo(indefinite):
    market, params = _market_and_params()
    datum = encode_action_datum(ACTION, indefinite=indefinite).hex()
    info = _action_info(market, params, datum=datum)
    record = parse_action(info, market)
    assert isinstance(record, LiqwidRecord)
    assert record.kind is LiqwidKind.ACTION
    assert record.out_ref == f"{TX_HASH}#3"
    assert record.market_id == market.market_id
    assert record.generation == NEW_GENERATION
    assert record.datum == ACTION
    assert record.datum.nonce == ACTION.nonce
    assert record.info is info


def test_parse_action_accepts_other_tokens_beside_the_action_token():
    market, params = _market_and_params("DJED")
    assets = {
        "lovelace": 2_000_000,
        market.underlying: 7_000_000,
        market.action_token_policy: 1,
    }
    assert parse_action(_action_info(market, params, assets=assets), market)


@pytest.mark.parametrize(
    "assets",
    [
        lambda p: {"lovelace": 2_000_000},
        lambda p: {"lovelace": 2_000_000, p: 2},
        lambda p: {"lovelace": 2_000_000, p: 1, p + "01": 1},
        lambda p: {"lovelace": 2_000_000, p + "01": 1},
    ],
    ids=["no_token", "quantity_two", "two_tokens", "named_token"],
)
def test_parse_action_needs_exactly_one_unnamed_action_token(assets):
    market, params = _market_and_params()
    held = assets(market.action_token_policy)
    assert parse_action(_action_info(market, params, assets=held), market) is None


def test_parse_action_rejects_another_markets_action_token():
    market, params = _market_and_params()
    other, _ = _market_and_params("DJED")
    assert other.action_token_policy != market.action_token_policy
    info = _action_info(other, params)
    assert parse_action(info, market) is None
    assert parse_action(info, other) is not None


@pytest.mark.parametrize(
    "datum",
    [
        "",
        "d87980",
        "zz",
        "ff",
        cbor2.dumps([[1, 2, 3, 4], None]).hex(),
        cbor2.dumps([[1, 2, 3, 4, b"\x01"], None]).hex(),
        cbor2.dumps([[1, 2, 3, 4, 5]]).hex(),
    ],
    ids=[
        "no_datum",
        "parked",
        "not_hex",
        "lone_break_byte",
        "four_deltas",
        "wrong_typed_delta",
        "no_nonce",
    ],
)
def test_parse_action_rejects_a_missing_or_malformed_datum(datum):
    market, params = _market_and_params()
    assert parse_action(_action_info(market, params, datum=datum), market) is None


def test_parse_action_refuses_a_market_without_an_action_token_policy():
    market, params = _market_and_params()
    info = _action_info(market, params)
    no_policy = dataclasses.replace(market, action_token_policy=None)
    assert parse_action(info, no_policy) is None


def test_an_action_utxo_is_not_a_read_side_record():
    market, params = _market_and_params()
    info = _action_info(market, params)
    assert parse_action(info, market) is not None
    assert parse_utxo(info) is None


def test_param_selector_is_the_routed_parameter_selector():
    deployment = default_deployment()
    routed = Counter(liqwid_selectors())
    built = Counter(
        param_selector(deployment, kind, market)
        for market in deployment.markets
        for kind in (LiqwidKind.MARKET_PARAMS, LiqwidKind.COLLATERAL_PARAMS)
    )
    assert built - routed == Counter()
    assert sum(built.values()) == 2 * len(deployment.markets)
    for market in deployment.markets:
        params = param_selector(deployment, LiqwidKind.MARKET_PARAMS, market)
        assert params.identity_policy == market.market_params_policy
        collateral = param_selector(deployment, LiqwidKind.COLLATERAL_PARAMS, market)
        assert collateral.identity_policy == market.collateral_params_policy
        for selector in (params, collateral):
            assert selector.payment_credential == deployment.param_credential
            assert selector.market_id == market.market_id
            assert selector.role == CONTEXT_ROLE


@pytest.mark.parametrize(
    "kind",
    [
        k
        for k in LiqwidKind
        if k not in {LiqwidKind.MARKET_PARAMS, LiqwidKind.COLLATERAL_PARAMS}
    ],
    ids=lambda k: k.value,
)
def test_param_selector_refuses_a_non_parameter_kind(kind):
    deployment = default_deployment()
    with pytest.raises(ValueError, match=kind.value):
        param_selector(deployment, kind, deployment.markets[0])


def test_state_selectors_are_the_markets_routed_state_selectors():
    deployment = default_deployment()
    routed = [s for s in liqwid_selectors() if s.kind is LiqwidKind.MARKET_STATE]
    collected = []
    for market in deployment.markets:
        selectors = state_selectors(deployment, market)
        assert selectors == [s for s in routed if s.market_id == market.market_id]
        assert [s.payment_credential for s in selectors] == list(
            market.market_state_validators,
        )
        assert {s.identity_policy for s in selectors} == {market.market_state_policy}
        assert {s.role for s in selectors} == {VIEW_ROLE}
        collected.extend(selectors)
    assert collected == routed
