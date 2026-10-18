"""Liqwid V2 oracle feeds: parsing, per-asset selection and lovelace prices."""

from __future__ import annotations

import subprocess
import sys
from fractions import Fraction
from itertools import permutations
from typing import Any

import cbor2

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import ORACLE_TOKEN_POLICY
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.oracles import LiqwidFeed
from charli3_dendrite.lending.liqwid.oracles import LiqwidOracleResolver
from charli3_dendrite.lending.liqwid.oracles import liqwid_price_map
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.liqwid.oracles import select_feeds
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import get_resolver
from charli3_dendrite.lending.oracles.qtoken import receipt_prices
from charli3_dendrite.lending.units import plutus_fields
from charli3_dendrite.lending.units import script_credential
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.liqwid.records import live_context
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.views import market

DEPLOYMENT = default_deployment()
VALIDATORS = {v.role: v.script_hash for v in DEPLOYMENT.oracle_validators}
TOKEN_UNIT = "cc" * 28 + "4142"
UPDATER = b"\x11" * 28
# The oracle token's circulating supply at the capture: 25 feeds at the hard-caps
# validator plus 7 pegs, one unspent token-bearing feed per priced asset.
LIVE_FEED_COUNT = 32


def feed_datum(
    unit: str,
    price: tuple[int, int],
    ts_ms: int,
    *,
    layout: int = 6,
    policy_wide: bool = False,
) -> str:
    """CBOR hex of ``[updater, asset, [num, den], ts_ms, validity(, caps)]``."""
    if policy_wide:
        asset = cbor2.CBORTag(121, [bytes.fromhex(unit)])
    elif unit == "lovelace":
        asset = cbor2.CBORTag(122, [[b"", b""]])
    else:
        policy, name = bytes.fromhex(unit[:56]), bytes.fromhex(unit[56:])
        asset = cbor2.CBORTag(122, [[policy, name]])
    fields: list[Any] = [UPDATER, asset, list(price), ts_ms, 60_000]
    if layout == 6:
        fields.append(cbor2.CBORTag(122, []))
    return cbor2.dumps(fields).hex()


def feed_info(
    out_ref: str,
    datum_cbor: str,
    *,
    role: str = "hard_caps",
    token: bool = True,
) -> PoolStateInfo:
    """A backend-shaped UTxO at an oracle validator, holding the oracle token or not."""
    tx_hash, index = out_ref.split("#")
    assets = {"lovelace": 2_000_000}
    if token:
        assets[ORACLE_TOKEN_POLICY] = 1
    return PoolStateInfo(
        address=script_payment_address(VALIDATORS[role]),
        tx_hash=tx_hash,
        tx_index=int(index),
        block_time=1_790_000_000,
        block_index=0,
        block_hash="",
        datum_hash="",
        datum_cbor=datum_cbor,
        assets=Assets(root=assets),
        plutus_v2=False,
    )


def make_feed(
    out_ref: str,
    asset: str = "lovelace",
    price: tuple[int, int] = (189, 500),
    ts_ms: int = 1_000,
    *,
    policy_wide: bool = False,
) -> LiqwidFeed:
    """A feed value built directly, for selection tests."""
    return LiqwidFeed(
        out_ref=out_ref,
        asset_unit=asset,
        policy_wide=policy_wide,
        usd_num=price[0],
        usd_den=price[1],
        ts_ms=ts_ms,
        updater=UPDATER.hex(),
        field4=60_000,
        caps=None,
    )


def genuine_records(*, live: bool | None = None) -> list[dict[str, Any]]:
    """Captured oracle records that hold the oracle token."""
    return [
        rec
        for rec in records_of("oracle", live=live)
        if rec["assets"].get(ORACLE_TOKEN_POLICY, 0) >= 1
    ]


def captured_feeds(*, live: bool | None = None) -> list[LiqwidFeed]:
    """Every captured genuine feed, parsed."""
    feeds = [parse_feed(record_info(rec)) for rec in genuine_records(live=live)]
    assert all(feed is not None for feed in feeds)
    return [feed for feed in feeds if feed is not None]


def raw_fields(rec: dict[str, Any]) -> list[Any]:
    """Positional fields of a captured record's datum, straight from the CBOR."""
    return plutus_fields(cbor2.loads(bytes.fromhex(rec["datum_cbor"])))


def test_captured_feeds_parse_in_both_layouts_and_match_the_raw_fields():
    records = genuine_records()
    assert {len(raw_fields(rec)) for rec in records} == {5, 6}
    assert {script_credential(rec["address"]) for rec in records} == set(
        VALIDATORS.values(),
    )
    for rec in records:
        feed = parse_feed(record_info(rec))
        assert feed is not None, rec["out_ref"]
        fields = raw_fields(rec)
        assert feed.out_ref == rec["out_ref"]
        assert feed.updater == fields[0].hex()
        assert (feed.usd_num, feed.usd_den) == tuple(plutus_fields(fields[2]))
        assert feed.ts_ms == fields[3]
        assert feed.field4 == fields[4]


def test_synthetic_list5_and_list6_feeds_parse_alike():
    out_ref = "aa" * 32 + "#0"
    five = parse_feed(feed_info(out_ref, feed_datum(TOKEN_UNIT, (3, 7), 5, layout=5)))
    six = parse_feed(feed_info(out_ref, feed_datum(TOKEN_UNIT, (3, 7), 5)))
    assert five is not None
    assert six is not None
    assert five == six
    assert (five.asset_unit, five.policy_wide, five.asset_key) == (
        TOKEN_UNIT,
        False,
        TOKEN_UNIT,
    )
    assert (five.usd_num, five.usd_den, five.ts_ms, five.field4) == (3, 7, 5, 60_000)
    assert five.updater == UPDATER.hex()


def test_pegs_parse_in_both_shapes_and_are_selected_like_any_feed():
    pegs = [
        rec
        for rec in genuine_records()
        if script_credential(rec["address"]) == VALIDATORS["pegs"]
    ]
    assert {len(raw_fields(rec)) for rec in pegs} == {5, 6}
    peg_feeds = [parse_feed(record_info(rec)) for rec in pegs]
    assert {(1, 1), (1, 100)} <= {(f.usd_num, f.usd_den) for f in peg_feeds if f}
    live_pegs = [
        parse_feed(record_info(rec))
        for rec in genuine_records(live=True)
        if script_credential(rec["address"]) == VALIDATORS["pegs"]
    ]
    assert live_pegs
    selected = select_feeds(captured_feeds(live=True))
    for peg in live_pegs:
        assert peg is not None
        assert selected[peg.asset_key] == peg


def test_constr0_feed_is_policy_wide_never_lovelace_and_never_priced():
    wide = [feed for feed in captured_feeds() if feed.policy_wide]
    assert wide
    for feed in wide:
        assert len(feed.asset_unit) == 56
        assert feed.asset_unit != "lovelace"
        assert feed.asset_key == "policy:" + feed.asset_unit
    prices = liqwid_price_map(captured_feeds())
    tokens = {token for token, _quote in prices.prices}
    assert not tokens & {feed.asset_unit for feed in wide}
    assert not tokens & {feed.asset_key for feed in wide}
    assert not select_feeds(captured_feeds(live=True))["lovelace"].policy_wide

    policy = "dd" * 28
    wide_datum = feed_datum(policy, (10_000, 1), 9, policy_wide=True)
    synthetic = parse_feed(feed_info("ab" * 32 + "#0", wide_datum))
    assert synthetic is not None
    assert synthetic.policy_wide
    assert synthetic.asset_key == "policy:" + policy
    ada = make_feed("ab" * 32 + "#1")
    assert set(liqwid_price_map([synthetic, ada]).prices) == {("lovelace", "lovelace")}


def test_decoys_without_the_oracle_token_are_rejected_by_the_token_gate():
    decoys = [rec for rec in records_of("reject") if rec["label"] == "oracle_decoy"]
    assert len(decoys) == 4
    for rec in decoys:
        assert rec["assets"].get(ORACLE_TOKEN_POLICY, 0) == 0
        assert script_credential(rec["address"]) == VALIDATORS["hard_caps"]
        assert parse_feed(record_info(rec)) is None
        with_token = Assets(root={**rec["assets"], ORACLE_TOKEN_POLICY: 1})
        assert parse_feed(record_info(rec, assets=with_token)) is not None
    datum = feed_datum(TOKEN_UNIT, (3, 7), 5)
    assert parse_feed(feed_info("aa" * 32 + "#0", datum, token=False)) is None


def test_two_oracle_tokens_are_not_a_feed():
    info = feed_info("aa" * 32 + "#0", feed_datum(TOKEN_UNIT, (3, 7), 5))
    assert parse_feed(info) is not None
    doubled = Assets(root={**info.assets.root, ORACLE_TOKEN_POLICY: 2})
    assert parse_feed(info.model_copy(update={"assets": doubled})) is None


def test_an_extra_named_token_under_the_oracle_policy_is_not_a_feed():
    info = feed_info("aa" * 32 + "#0", feed_datum(TOKEN_UNIT, (3, 7), 5))
    named = Assets(root={**info.assets.root, ORACLE_TOKEN_POLICY + "4142": 1})
    assert parse_feed(info.model_copy(update={"assets": named})) is None
    only_named = {k: v for k, v in named.root.items() if k != ORACLE_TOKEN_POLICY}
    renamed = info.model_copy(update={"assets": Assets(root=only_named)})
    assert parse_feed(renamed) is None


def test_wrong_credential_or_undecodable_datum_is_rejected():
    rec = genuine_records(live=True)[0]
    for script_hash in (DEPLOYMENT.param_credential, "ab" * 28):
        moved = record_info(rec, address=script_payment_address(script_hash))
        assert parse_feed(moved) is None
    assert parse_feed(record_info(rec, datum_cbor="")) is None
    assert parse_feed(record_info(rec, datum_cbor="9f01ff")) is None
    assert parse_feed(record_info(rec, datum_cbor="zz")) is None


def test_select_feeds_keeps_the_freshest_feed_per_asset():
    older = make_feed("01" * 32 + "#0", TOKEN_UNIT, (1, 2), ts_ms=100)
    newest = make_feed("02" * 32 + "#0", TOKEN_UNIT, (1, 3), ts_ms=300)
    middle = make_feed("03" * 32 + "#0", TOKEN_UNIT, (1, 4), ts_ms=200)
    ada = make_feed("04" * 32 + "#0")
    for order in permutations((older, newest, middle, ada)):
        assert select_feeds(order) == {TOKEN_UNIT: newest, "lovelace": ada}


def test_select_feeds_on_captured_history_matches_the_freshest_per_asset():
    feeds = captured_feeds()
    selected = select_feeds(feeds)
    assert set(selected) == {feed.asset_key for feed in feeds}
    for key, chosen in selected.items():
        assert chosen.ts_ms == max(f.ts_ms for f in feeds if f.asset_key == key)
    live = captured_feeds(live=True)
    assert len(live) == LIVE_FEED_COUNT
    assert len(select_feeds(live)) == len(live)


def test_equal_timestamps_select_the_same_out_ref_in_either_order():
    tx = "aa" * 32
    first = make_feed(tx + "#2", TOKEN_UNIT, (1, 2), ts_ms=500)
    second = make_feed(tx + "#10", TOKEN_UNIT, (1, 3), ts_ms=500)
    assert select_feeds([first, second])[TOKEN_UNIT].out_ref == tx + "#10"
    assert select_feeds([second, first])[TOKEN_UNIT].out_ref == tx + "#10"
    other_tx = make_feed("bb" * 32 + "#0", TOKEN_UNIT, (1, 4), ts_ms=500)
    for order in permutations((first, second, other_tx)):
        assert select_feeds(order)[TOKEN_UNIT].out_ref == "bb" * 32 + "#0"


def test_price_map_converts_usd_per_million_to_lovelace_exactly():
    ada = make_feed("01" * 32 + "#0", "lovelace", (189, 500), ts_ms=7)
    peg = make_feed("02" * 32 + "#0", TOKEN_UNIT, (1, 1), ts_ms=9)
    prices = liqwid_price_map([ada, peg])
    lovelace = prices.require("lovelace")
    assert (lovelace.num, lovelace.denom) == (189 * 500, 500 * 189)
    assert Fraction(lovelace.num, lovelace.denom) == 1
    token = prices.require(TOKEN_UNIT)
    assert (token.num, token.denom) == (1 * 500, 1 * 189)
    assert token.quote == "lovelace"
    assert token.source is OracleSource.LIQWID
    assert token.valid_from == 9
    assert token.valid_to is None
    assert lovelace.valid_from == 7


def test_price_map_on_captured_live_feeds_follows_the_hand_formula():
    live = captured_feeds(live=True)
    ada = select_feeds(live)["lovelace"]
    prices = liqwid_price_map(live)
    priced = [f for f in live if not f.policy_wide and f.usd_num > 0]
    assert priced
    for feed in priced:
        price = prices.require(feed.asset_unit)
        assert price.num == feed.usd_num * ada.usd_den
        assert price.denom == feed.usd_den * ada.usd_num
        assert price.valid_from == feed.ts_ms
    assert len(prices.prices) == len(priced)


def test_zero_price_or_missing_ada_feed_gives_no_entry():
    ada = make_feed("01" * 32 + "#0")
    zero = make_feed("02" * 32 + "#0", TOKEN_UNIT, (0, 1))
    zero_den = make_feed("03" * 32 + "#0", "ee" * 28 + "41", (5, 0))
    prices = liqwid_price_map([ada, zero, zero_den])
    assert set(prices.prices) == {("lovelace", "lovelace")}
    assert liqwid_price_map([zero]).prices == {}
    zero_ada = make_feed("04" * 32 + "#0", "lovelace", (0, 1))
    peg = make_feed("05" * 32 + "#0", TOKEN_UNIT, (1, 1))
    assert liqwid_price_map([zero_ada, peg]).prices == {}


def ref_for(component: MarketComponents) -> OracleRef:
    """The qToken price reference a Liqwid collateral term carries."""
    return OracleRef(
        source=OracleSource.LIQWID,
        token=component.qtoken_policy,
        extra={
            "underlying": component.underlying,
            "market_state": component.market_state_policy,
        },
    )


def live_states() -> dict[str, tuple[PoolStateInfo, MarketStateDatum]]:
    """Captured live MarketState UTxO and datum per market id."""
    states: dict[str, tuple[PoolStateInfo, MarketStateDatum]] = {}
    for info in live_context().utxos:
        record = parse_utxo(info)
        if record is None or record.kind is not LiqwidKind.MARKET_STATE:
            continue
        assert isinstance(record.datum, MarketStateDatum)
        assert record.market_id is not None
        states[record.market_id] = (record.info, record.datum)
    return states


def token_market() -> MarketComponents:
    """A live market whose underlying is a native token, not ADA."""
    states = live_states()
    return next(
        m
        for m in DEPLOYMENT.markets
        if m.underlying != "lovelace" and m.market_state_policy in states
    )


def test_importing_the_module_registers_the_resolver():
    resolver = get_resolver(OracleSource.LIQWID)
    assert isinstance(resolver, LiqwidOracleResolver)
    assert resolver.source is OracleSource.LIQWID


def test_shared_lending_import_alone_does_not_register_the_resolver():
    code = (
        "import charli3_dendrite.lending; "
        "from charli3_dendrite.lending.oracles.models import "
        "OracleSource, resolver_sources; "
        "assert OracleSource.LIQWID not in resolver_sources(); "
        "import charli3_dendrite.lending.liqwid.oracles; "
        "assert OracleSource.LIQWID in resolver_sources()"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_selectors_cover_each_oracle_validator_and_each_market_state_once():
    first, second = DEPLOYMENT.markets[0], DEPLOYMENT.markets[1]
    refs = [
        ref_for(first),
        ref_for(second),
        ref_for(first),
        OracleRef(source=OracleSource.LIQWID, token="ab" * 28),
        OracleRef(
            source=OracleSource.LIQWID,
            token="ab" * 28,
            extra={"underlying": "lovelace", "market_state": "ef" * 28},
        ),
    ]
    oracle = [
        PoolSelector(
            addresses=[script_payment_address(v.script_hash)],
            assets=[ORACLE_TOKEN_POLICY],
        )
        for v in DEPLOYMENT.oracle_validators
    ]
    states = [
        PoolSelector(
            addresses=[script_payment_address(h) for h in m.market_state_validators],
            assets=[m.market_state_policy],
        )
        for m in (first, second)
    ]
    assert LiqwidOracleResolver().selectors(refs) == oracle + states
    assert LiqwidOracleResolver(DEPLOYMENT).selectors([]) == oracle


def test_resolver_on_live_records_is_s9_times_the_underlying_lovelace_price():
    utxos = PoolStateList(root=list(live_context().utxos))
    feeds = [feed for feed in (parse_feed(info) for info in utxos) if feed]
    prices = liqwid_price_map(feeds)
    states = live_states()
    # Every live market has a captured MarketState (35 live markets at the capture).
    assert len(states) == 35
    resolver = LiqwidOracleResolver()
    for market_id, (_info, state) in states.items():
        component = DEPLOYMENT.market(market_id)
        got = resolver.resolve(ref_for(component), utxos)
        assert got is not None, component.name
        assert got.token == component.qtoken_policy
        assert got.quote == "lovelace"
        assert got.source is OracleSource.LIQWID
        s9 = Fraction(*state.qtoken_rate)
        underlying = prices.require(component.underlying)
        value = Fraction(got.num, got.denom)
        assert value == s9 * Fraction(underlying.num, underlying.denom)
        hop = receipt_prices(
            [
                market(
                    market_id,
                    borrow_unit=component.underlying,
                    receipt_unit=component.qtoken_policy,
                    receipt_rate=s9,
                ),
            ],
            prices,
        ).require(component.qtoken_policy)
        assert Fraction(hop.num, hop.denom) == value


def test_resolver_follows_the_hand_formula_on_synthetic_feeds():
    component = token_market()
    state_info, state = live_states()[component.market_state_policy]
    ada = feed_info("01" * 32 + "#0", feed_datum("lovelace", (189, 500), 5))
    token = feed_info("02" * 32 + "#0", feed_datum(component.underlying, (3, 7), 5))
    got = LiqwidOracleResolver().resolve(
        ref_for(component),
        PoolStateList(root=[state_info, ada, token]),
    )
    assert got is not None
    s9_num, s9_den = state.qtoken_rate
    assert (got.num, got.denom) == (s9_num * 3 * 500, s9_den * 7 * 189)


def test_zero_price_feed_resolves_to_none():
    component = token_market()
    state_info, _state = live_states()[component.market_state_policy]
    ada = feed_info("01" * 32 + "#0", feed_datum("lovelace", (189, 500), 5))
    zero = feed_info("02" * 32 + "#0", feed_datum(component.underlying, (0, 1), 5))
    utxos = PoolStateList(root=[state_info, ada, zero])
    assert LiqwidOracleResolver().resolve(ref_for(component), utxos) is None
    prices = liqwid_price_map(
        [feed for feed in (parse_feed(info) for info in utxos) if feed],
    )
    assert prices.get(component.underlying) is None
    zero_ada = feed_info("03" * 32 + "#0", feed_datum("lovelace", (0, 1), 5))
    token = feed_info("04" * 32 + "#0", feed_datum(component.underlying, (3, 7), 5))
    no_ada = PoolStateList(root=[state_info, zero_ada, token])
    assert LiqwidOracleResolver().resolve(ref_for(component), no_ada) is None


def test_resolver_returns_none_when_an_input_is_missing():
    component = token_market()
    state_info, _state = live_states()[component.market_state_policy]
    ada = feed_info("01" * 32 + "#0", feed_datum("lovelace", (189, 500), 5))
    token = feed_info("02" * 32 + "#0", feed_datum(component.underlying, (3, 7), 5))
    resolver = LiqwidOracleResolver()
    ref = ref_for(component)
    assert resolver.resolve(ref, PoolStateList(root=[state_info, ada, token]))
    assert resolver.resolve(ref, PoolStateList(root=[ada, token])) is None
    assert resolver.resolve(ref, PoolStateList(root=[state_info, token])) is None
    assert resolver.resolve(ref, PoolStateList(root=[state_info, ada])) is None
    decoy = feed_info(
        "03" * 32 + "#0",
        feed_datum(component.underlying, (3, 7), 5),
        token=False,
    )
    assert resolver.resolve(ref, PoolStateList(root=[state_info, ada, decoy])) is None
    moved = state_info.model_copy(
        update={"address": script_payment_address("ab" * 28)},
    )
    assert resolver.resolve(ref, PoolStateList(root=[moved, ada, token])) is None
    full = PoolStateList(root=[state_info, ada, token])
    wrong_token = ref.model_copy(update={"token": "ab" * 28})
    assert resolver.resolve(wrong_token, full) is None
    unknown_market = ref.model_copy(
        update={"extra": {**ref.extra, "market_state": "ef" * 28}},
    )
    assert resolver.resolve(unknown_market, full) is None
    assert resolver.resolve(ref.model_copy(update={"extra": {}}), full) is None
    wrong_underlying = ref.model_copy(
        update={"extra": {**ref.extra, "underlying": "lovelace"}},
    )
    assert resolver.resolve(wrong_underlying, full) is None
