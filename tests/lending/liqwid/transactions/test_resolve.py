"""Live resolution of Liqwid V2 scripts, action UTxOs, loans, records and feeds (offline).

Every resolver runs against :class:`FixtureBackend`, which serves the captured live
records and canned out-ref rows; reference-script bytes are not bundled, so each
reference UTxO carries placeholder bytes typed with the deployment's language and
the script-hash check reads the deployment's own hash for that out-ref.
"""

import dataclasses
import random
from collections import Counter

import cbor2
import pytest
from pycardano import Address
from pycardano import Network
from pycardano import PlutusV2Script
from pycardano import ScriptHash
from pycardano import VerificationKeyHash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import decode_market_params
from charli3_dendrite.lending.liqwid.datums import encode_action_datum
from charli3_dendrite.lending.liqwid.deployment import ORACLE_TOKEN_POLICY
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.deployment import staking_roles
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import action_selector
from charli3_dendrite.lending.liqwid.indexing import chain_position
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.transactions import resolve
from charli3_dendrite.lending.liqwid.transactions.errors import ActionUtxoSpentError
from charli3_dendrite.lending.liqwid.transactions.errors import CapExceededError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MissingFeedError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    UnsupportedCollateralError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedLoanError
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
from charli3_dendrite.lending.liqwid.transactions.resolve import choose_action_utxo
from charli3_dendrite.lending.liqwid.transactions.resolve import collateral_link
from charli3_dendrite.lending.liqwid.transactions.resolve import (
    collateral_params_records,
)
from charli3_dendrite.lending.liqwid.transactions.resolve import record_from_utxo
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_action_utxos
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_feeds
from charli3_dendrite.lending.liqwid.transactions.resolve import (
    resolve_health_inputs,
)
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
from charli3_dendrite.lending.liqwid.transactions.resolve import (
    resolve_market_records,
)
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_now_ms
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_script_refs
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import plutus_script
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.units import policy_of
from tests.lending.liqwid.records import parsed_records
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.backend import live_records

TX = "ab" * 32
# A placeholder script body: the resolvers never execute or hash it (see the module
# docstring), they only carry it.
SCRIPT_HEX = "4e4d01000033222220051200120011"
# The seven live markets whose MarketParams p[20] is not the newest action
# validator the deployment lists.
OLD_ACTION_MARKETS = ("AGIX", "Ada", "DJED", "IUSD", "SHEN", "USDC", "USDT")
# Legacy-generation loans live at the capture.
LEGACY_LOANS = (
    "759224e9aebdee551f5c99f94e40ab3755833c43538bd9b6c9520562d11fcf70#1",
    "dd5f2b90220d00e8c364fbdae25a95c03a203943a8862fd7f753104ced5c4a5c#2",
    "ff2db16f9af30639aee9f2cb989380063ea45057518a2714e1e8e493a1a4948c#2",
    "aaefaabb101a3e5645b17d03b8a4240ae7947f43d168c6a9dc8203d76bfd2055#1",
    "ca7a4b568d236b0ab7bf4c4d6a5a0190b5089e13ee9783b19af1616042a44a56#1",
)
# The policy of the policy-wide Ada CollateralParams record and oracle feed.
POLICY_WIDE = "1f3dd1b685f0d87eaf92120aca3cf56da6ed91e2dbc32a95a26bce4a"
STAKE_KEYS = ("f61e5cf9" * 7, "fda79c6a" * 7)


# --- shared helpers ----------------------------------------------------------


def _live_params():
    """Each market with its live MarketParams datum, in capture order."""
    deployment = default_deployment()
    return [
        (deployment.market(rec["market_id"]), decode_market_params(rec["datum_cbor"]))
        for rec in records_of("market_params", live=True)
        if rec["label"] is None
    ]


def _market_and_params(name):
    return next((m, p) for m, p in _live_params() if m.name == name)


def _ref(out_ref):
    tx_hash, _, index = out_ref.partition("#")
    return (tx_hash, int(index))


def _text(utxo):
    return f"{utxo.out_ref[0]}#{utxo.out_ref[1]}"


def _reference_utxo(ref, *, language=None):
    return Utxo(
        address=ref.address,
        lovelace=20_000_000,
        assets=[],
        datum=None,
        ref_script=SCRIPT_HEX,
        ref_script_type=language or ref.language,
        out_ref=_ref(ref.out_ref),
    )


def _reference_utxos():
    return [
        _reference_utxo(ref) for ref in default_deployment().reference_utxos.values()
    ]


@pytest.fixture()
def deployment_hashes(monkeypatch):
    """Each reference UTxO hashes to the script the deployment lists for it."""
    by_ref = {}
    for script_hash, ref in default_deployment().reference_utxos.items():
        assert by_ref.setdefault(ref.out_ref, script_hash) == script_hash
    monkeypatch.setattr(resolve, "script_hash_of", lambda u: by_ref[_text(u)])
    return by_ref


def _script_backend(**kwargs):
    return FixtureBackend(utxos=_reference_utxos(), **kwargs)


def _action_address(params, stake):
    return Address(
        payment_part=ScriptHash(bytes.fromhex(params.action_validator)),
        staking_part=VerificationKeyHash(bytes.fromhex(stake)),
        network=Network.MAINNET,
    ).encode()


def _action_info(market, params, index, *, token=True, **kw):
    assets = {"lovelace": 50_000_000}
    if token:
        assets[market.action_token_policy] = 1
    datum = ActionDatum(index, -index, 0, 0, 0, cbor2.CBORTag(121, [index]))
    values = {
        "address": _action_address(params, STAKE_KEYS[index % 2]),
        "tx_hash": TX,
        "tx_index": index,
        "block_time": 1_790_000_000,
        "block_index": 0,
        "block_hash": "",
        "datum_hash": "",
        "datum_cbor": encode_action_datum(datum).hex(),
        "assets": Assets(root=assets),
        "plutus_v2": True,
    }
    values.update(kw)
    return PoolStateInfo(**values)


def _action(index, *, lovelace=50_000_000, assets=()):
    datum = ActionDatum(0, 0, 0, 0, 0, cbor2.CBORTag(121, []))
    utxo = Utxo(
        address="addr_test",
        lovelace=lovelace,
        assets=list(assets),
        datum=None,
        out_ref=(TX, index),
    )
    return ActionUtxo(utxo=utxo, datum=datum)


# --- choose_action_utxo ------------------------------------------------------


def _passes(action):
    return None


def _needs(amount):
    def check(action):
        held = action.underlying_amount("lovelace")
        if held < amount:
            raise InsufficientLiquidityError(amount, held)

    return check


def test_choice_is_reproducible_with_a_seeded_rng():
    utxos = [_action(i) for i in range(6)]
    picks = [
        choose_action_utxo(utxos, check=_passes, rng=random.Random(0)).out_ref
        for _ in range(3)
    ]
    assert len(set(picks)) == 1
    rng = random.Random(0)
    first = [
        choose_action_utxo(utxos, check=_passes, rng=rng).out_ref for _ in range(20)
    ]
    rng = random.Random(0)
    again = [
        choose_action_utxo(utxos, check=_passes, rng=rng).out_ref for _ in range(20)
    ]
    assert first == again


def test_choice_never_returns_an_excluded_or_failing_utxo():
    utxos = [_action(i, lovelace=(i + 1) * 10_000_000) for i in range(6)]
    excluded = {f"{TX}#5"}
    rng = random.Random(0)
    seen = Counter(
        choose_action_utxo(
            utxos,
            check=_needs(25_000_000),
            exclude=excluded,
            rng=rng,
        ).out_ref
        for _ in range(400)
    )
    # Indexes 0 and 1 hold too little, 5 is excluded: 2, 3 and 4 are all drawn.
    assert set(seen) == {f"{TX}#2", f"{TX}#3", f"{TX}#4"}


def test_choice_without_an_rng_uses_the_system_random():
    utxos = [_action(i) for i in range(3)]
    assert choose_action_utxo(utxos, check=_passes) in utxos


def test_a_pin_wins_and_is_checked():
    utxos = [_action(i, lovelace=(i + 1) * 10_000_000) for i in range(4)]
    pinned = choose_action_utxo(utxos, check=_passes, pin=f"{TX}#1")
    assert pinned is utxos[1]
    with pytest.raises(InsufficientLiquidityError) as info:
        choose_action_utxo(utxos, check=_needs(25_000_000), pin=f"{TX}#1")
    assert (info.value.needed, info.value.available) == (25_000_000, 20_000_000)


def test_an_absent_pin_is_a_spent_action_utxo():
    utxos = [_action(i) for i in range(3)]
    with pytest.raises(ActionUtxoSpentError) as info:
        choose_action_utxo(utxos, check=_passes, pin=f"{TX}#7")
    assert info.value.out_ref == f"{TX}#7"


def test_an_excluded_pin_is_a_spent_action_utxo():
    utxos = [_action(i) for i in range(3)]
    with pytest.raises(ActionUtxoSpentError) as info:
        choose_action_utxo(utxos, check=_passes, pin=f"{TX}#1", exclude={f"{TX}#1"})
    assert info.value.out_ref == f"{TX}#1"


def test_no_eligible_utxo_re_raises_the_last_check_error():
    utxos = [_action(i, lovelace=(i + 1) * 10_000_000) for i in range(3)]
    with pytest.raises(InsufficientLiquidityError) as info:
        choose_action_utxo(utxos, check=_needs(90_000_000))
    assert (info.value.needed, info.value.available) == (90_000_000, 30_000_000)

    def capped(action):
        raise CapExceededError("supply")

    with pytest.raises(CapExceededError):
        choose_action_utxo(utxos, check=capped)


@pytest.mark.parametrize("excluded", [True, False])
def test_nothing_to_choose_from_is_no_liquidity(excluded):
    utxos = [_action(i) for i in range(2)] if excluded else []
    exclude = {f"{TX}#0", f"{TX}#1"} if excluded else ()
    with pytest.raises(InsufficientLiquidityError) as info:
        choose_action_utxo(utxos, check=_passes, exclude=exclude)
    assert (info.value.needed, info.value.available) == (0, 0)


def test_an_action_utxo_names_its_out_ref_and_underlying():
    token = "cc" * 28 + "4e4d"
    action = _action(3, lovelace=7, assets=[("cc" * 28, "4e4d", 11)])
    assert action.out_ref == f"{TX}#3"
    assert action.underlying_amount("lovelace") == 7
    assert action.underlying_amount(token) == 11
    assert action.underlying_amount("dd" * 28) == 0


# --- resolve_action_utxos ----------------------------------------------------


def test_action_utxos_are_read_by_token_at_the_p20_credential():
    market, params = _market_and_params("Ada")
    other_market, other_params = _market_and_params("DJED")
    genuine = [_action_info(market, params, i) for i in (10, 2, 3)]
    no_token = _action_info(market, params, 4, token=False)
    elsewhere = _action_info(
        market,
        params,
        5,
        address=_action_address(other_params, STAKE_KEYS[0]),
    )
    spent = _action_info(market, params, 6)
    backend = FixtureBackend(records=[*genuine, no_token, elsewhere], rows=[spent])
    found = resolve_action_utxos(backend, market, params)
    assert [a.out_ref for a in found] == [f"{TX}#2", f"{TX}#3", f"{TX}#10"]
    assert all(isinstance(a.datum, ActionDatum) for a in found)
    assert found[0].utxo == utxo_from_info(genuine[1])
    assert found[0].datum.supply_delta == 2
    selector = action_selector(market, params)
    assert backend.calls == [
        {
            "addresses": (selector.address,),
            "assets": (market.action_token_policy,),
            "limit": 1000,
            "page": 0,
            "historical": False,
        },
    ]


class _AssetOnlyBackend(FixtureBackend):
    """Selects by asset alone, as a backend that ignores the address would."""

    def get_pool_utxos(self, addresses, assets=None, **kwargs):
        every = {info.address for info in self.records}
        return super().get_pool_utxos(sorted(every), assets, **kwargs)


def test_an_action_token_away_from_p20_is_not_an_action_utxo():
    market, params = _market_and_params("Ada")
    _, other_params = _market_and_params("DJED")
    genuine = _action_info(market, params, 1)
    elsewhere = _action_info(
        market,
        params,
        2,
        address=_action_address(other_params, STAKE_KEYS[0]),
    )
    backend = _AssetOnlyBackend(records=[genuine, elsewhere])
    found = resolve_action_utxos(backend, market, params)
    assert [a.out_ref for a in found] == [f"{TX}#1"]


def test_action_utxos_page_until_a_short_page():
    market, params = _market_and_params("Ada")
    infos = [_action_info(market, params, i) for i in range(5)]
    backend = FixtureBackend(records=infos)
    found = resolve_action_utxos(backend, market, params, page_size=2)
    assert [a.out_ref for a in found] == [f"{TX}#{i}" for i in range(5)]
    assert [call["page"] for call in backend.calls] == [0, 1, 2]
    assert {call["limit"] for call in backend.calls} == {2}


class _AddressOnlyBackend(FixtureBackend):
    """Selects by address alone, as a backend that ignores the asset filter would."""

    def get_pool_utxos(self, addresses, assets=None, **kwargs):
        return super().get_pool_utxos(addresses, None, **kwargs)


@pytest.mark.parametrize(
    ("backend_type", "unparsed"),
    [
        (_AddressOnlyBackend, {"token": False}),
        (FixtureBackend, {"datum_cbor": "00"}),
    ],
    ids=["token-less", "malformed-datum"],
)
def test_a_page_of_unparseable_utxos_does_not_end_the_scan(backend_type, unparsed):
    market, params = _market_and_params("Ada")
    junk = [_action_info(market, params, i, **unparsed) for i in (0, 1)]
    genuine = [_action_info(market, params, i) for i in (2, 3)]
    backend = backend_type(records=[*junk, *genuine])
    found = resolve_action_utxos(backend, market, params, page_size=2)
    assert [a.out_ref for a in found] == [f"{TX}#2", f"{TX}#3"]
    assert [call["page"] for call in backend.calls] == [0, 1, 2]


def test_action_utxos_of_a_market_without_an_action_token_policy_are_refused():
    market, params = _market_and_params("Ada")
    bare = dataclasses.replace(market, action_token_policy=None)
    with pytest.raises(UnsupportedMarketError) as info:
        resolve_action_utxos(FixtureBackend(), bare, params)
    assert info.value.market == "Ada"


# --- resolve_script_refs -----------------------------------------------------


def _live36():
    return [(m, p) for m, p in _live_params() if p.length >= 36]


def test_a_layout_shorter_than_36_fields_refuses_the_loan_scripts(deployment_hashes):
    market, params = _market_and_params("POL")
    assert params.length < 36
    with pytest.raises(UnsupportedMarketError) as info:
        resolve_script_refs(_script_backend(), market, params)
    assert info.value.market == "POL"
    refs = resolve_script_refs(_script_backend(), market, params, loans=False)
    deployment = default_deployment()
    assert (
        _text(refs.action) == deployment.reference_utxo(params.action_validator).out_ref
    )
    assert _text(refs.qtoken_policy) == (
        deployment.reference_utxo(market.qtoken_policy).out_ref
    )
    assert refs.loan_validator is None
    assert refs.loan_policy is None
    assert refs.borrow_staking is None
    assert refs.liquidation_staking is None


class _UnreadableBackend:
    """Fails on any attribute access, so any backend read fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"the backend was read: {name}")


def _without_loan_policy(name):
    market, params = _market_and_params(name)
    return dataclasses.replace(market, new_borrow_policy=None), params


def _without_staking_pair(name):
    market, params = _market_and_params(name)
    return market, dataclasses.replace(
        params, withdraw_zero=(params.loan_validator,) * 2
    )


@pytest.mark.parametrize(
    "case",
    [
        lambda: _market_and_params("POL"),
        lambda: _without_loan_policy("Ada"),
        lambda: _without_staking_pair("Ada"),
    ],
    ids=["short-layout", "no-loan-policy", "no-staking-pair"],
)
def test_a_market_unfit_for_loans_is_refused_before_any_backend_read(case):
    market, params = case()
    with pytest.raises(UnsupportedMarketError) as info:
        resolve_script_refs(_UnreadableBackend(), market, params)
    assert info.value.market == market.name


@pytest.mark.parametrize("name", OLD_ACTION_MARKETS)
def test_the_action_reference_is_the_one_for_p20(name, deployment_hashes):
    market, params = _market_and_params(name)
    assert params.action_validator != market.actions[-1]
    deployment = default_deployment()
    refs = resolve_script_refs(_script_backend(), market, params)
    assert (
        _text(refs.action) == deployment.reference_utxo(params.action_validator).out_ref
    )
    assert _text(refs.action) != deployment.reference_utxo(market.actions[-1]).out_ref


def test_a_liquidation_first_market_reads_borrow_staking_from_p34_1(deployment_hashes):
    market, params = _market_and_params("USDCx")
    first, second = params.withdraw_zero
    assert first in market.liquidation_staking
    deployment = default_deployment()
    refs = resolve_script_refs(_script_backend(), market, params)
    assert _text(refs.borrow_staking) == deployment.reference_utxo(second).out_ref
    assert _text(refs.liquidation_staking) == deployment.reference_utxo(first).out_ref


def test_every_live_market_resolves_six_plutus_v2_references(deployment_hashes):
    deployment = default_deployment()
    markets = _live36()
    assert len(markets) == 34
    for market, params in markets:
        refs = resolve_script_refs(_script_backend(), market, params)
        assert isinstance(refs, LiqwidScriptRefs)
        roles = staking_roles(market, params)
        expected = {
            "action": params.action_validator,
            "qtoken_policy": market.qtoken_policy,
            "loan_validator": params.loan_validator,
            "loan_policy": market.new_borrow_policy,
            "borrow_staking": roles.borrow,
            "liquidation_staking": roles.liquidation,
        }
        for field, script_hash in expected.items():
            utxo = getattr(refs, field)
            ref = deployment.reference_utxo(script_hash)
            assert _text(utxo) == ref.out_ref, (market.name, field)
            assert utxo.address == ref.address
            assert utxo.ref_script_type == "plutusV2", (market.name, field)
            assert isinstance(plutus_script(utxo), PlutusV2Script)


def test_a_reference_utxo_of_another_language_is_refused(deployment_hashes):
    market, params = _market_and_params("Ada")
    action_ref = default_deployment().reference_utxo(params.action_validator)
    utxos = [
        _reference_utxo(ref, language="plutusV3" if ref == action_ref else None)
        for ref in default_deployment().reference_utxos.values()
    ]
    with pytest.raises(UnsupportedMarketError, match=action_ref.out_ref):
        resolve_script_refs(FixtureBackend(utxos=utxos), market, params)


def test_a_reference_utxo_holding_another_script_is_refused(monkeypatch):
    market, params = _market_and_params("Ada")
    monkeypatch.setattr(resolve, "script_hash_of", lambda u: "00" * 28)
    with pytest.raises(UnsupportedMarketError, match="hashes to"):
        resolve_script_refs(_script_backend(), market, params)


def test_a_spent_reference_utxo_is_refused(deployment_hashes):
    market, params = _market_and_params("Ada")
    action_ref = default_deployment().reference_utxo(params.action_validator)
    live = [u for u in _reference_utxos() if _text(u) != action_ref.out_ref]
    gone = [u for u in _reference_utxos() if _text(u) == action_ref.out_ref]
    backend = FixtureBackend(utxos=live, spent=gone)
    with pytest.raises(UnsupportedMarketError, match=action_ref.out_ref):
        resolve_script_refs(backend, market, params)


# --- market records ----------------------------------------------------------


def test_collateral_params_records_keep_the_newest_per_unit():
    deployment = default_deployment()
    ada = deployment.market_by_name("Ada")
    records = [record for _, record in parsed_records("collateral_params")]
    chosen = collateral_params_records(records, ada)
    live = {
        record.datum.collateral: record
        for rec, record in parsed_records("collateral_params", live=True)
        if record.market_id == ada.market_id and not record.datum.policy_wide
    }
    assert chosen == live
    assert POLICY_WIDE not in chosen
    # The Ada qToken record was replaced once: the newer record wins.
    older = [
        record
        for record in records
        if record.market_id == ada.market_id
        and record.datum.collateral == ada.qtoken_policy
    ]
    assert len(older) == 2
    newest = max(older, key=lambda r: chain_position(r.info))
    assert chosen[ada.qtoken_policy] == newest
    assert chosen[ada.qtoken_policy].out_ref == newest.out_ref


def test_collateral_params_records_ignore_other_markets_and_kinds():
    deployment = default_deployment()
    ada = deployment.market_by_name("Ada")
    djed = [
        record
        for _, record in parsed_records("collateral_params", live=True)
        if record.market_id != ada.market_id
    ]
    states = [record for _, record in parsed_records("market_state", live=True)]
    assert collateral_params_records([*djed, *states], ada) == {}


def test_market_records_come_from_one_fetch():
    deployment = default_deployment()
    markets = [deployment.market_by_name("Ada"), deployment.market_by_name("DJED")]
    backend = FixtureBackend(records=live_records())
    found = resolve_market_records(backend, markets)
    assert sorted(found) == ["Ada", "DJED"]
    for market in markets:
        records = found[market.name]
        assert isinstance(records, MarketRecords)
        assert records.market == market
        assert isinstance(records.params_datum, MarketParamsDatum)
        assert isinstance(records.state_datum, MarketStateDatum)
        assert records.params.kind is LiqwidKind.MARKET_PARAMS
        assert records.state.kind is LiqwidKind.MARKET_STATE
        assert records.params.market_id == market.market_id
        assert records.state.market_id == market.market_id
        assert all(
            isinstance(r.datum, CollateralParamsDatum)
            for r in records.collateral_params.values()
        )
        assert records.collateral_params
    # Each address is read once: the parameter address serves both markets.
    addresses = [call["addresses"] for call in backend.calls]
    assert len(addresses) == len(set(addresses))
    param_address = [a for a in addresses if a[0] == _param_address(deployment)]
    assert len(param_address) == 1


def _param_address(deployment):
    from charli3_dendrite.lending.units import script_payment_address

    return script_payment_address(deployment.param_credential)


def test_a_market_without_a_live_state_is_refused():
    ada = default_deployment().market_by_name("Ada")
    backend = FixtureBackend(records=live_records("market_params", "collateral_params"))
    with pytest.raises(UnsupportedMarketError) as info:
        resolve_market_records(backend, [ada])
    assert info.value.market == "Ada"


def test_a_market_without_live_params_is_refused():
    ada = default_deployment().market_by_name("Ada")
    backend = FixtureBackend(records=live_records("market_state"))
    with pytest.raises(UnsupportedMarketError) as info:
        resolve_market_records(backend, [ada])
    assert info.value.market == "Ada"


def test_record_from_utxo_reads_a_resolved_utxo():
    for kind in ("market_params", "market_state", "loan"):
        rec, record = parsed_records(kind, live=True)[0]
        again = record_from_utxo(utxo_from_info(record_info(rec)))
        assert again == record
    dust = Utxo(
        address=_param_address(default_deployment()),
        lovelace=2_000_000,
        assets=[],
        datum=None,
        out_ref=(TX, 0),
    )
    assert record_from_utxo(dust) is None


# --- resolve_loan ------------------------------------------------------------


def _loan_backend(*, spent=()):
    utxos = [
        utxo_from_info(record_info(rec))
        for kind in ("loan", "market_state")
        for rec in records_of(kind, live=True)
    ]
    return FixtureBackend(utxos=utxos, spent=spent)


@pytest.mark.parametrize("out_ref", LEGACY_LOANS)
def test_a_legacy_generation_loan_is_refused(out_ref):
    with pytest.raises(UnsupportedLoanError) as info:
        resolve_loan(_loan_backend(), out_ref)
    assert info.value.out_ref == out_ref


def test_a_legacy_loan_token_at_a_new_validator_is_refused():
    deployment = default_deployment()
    rec, record = next(
        (rec, record)
        for rec, record in parsed_records("loan", live=True)
        if record.generation == "new"
    )
    market = deployment.market(record.market_id)
    token = next(u for u in rec["assets"] if u.startswith(market.new_borrow_policy))
    assets = dict(rec["assets"])
    name = token[56:]
    assets.pop(token)
    assets[market.borrow_policy + name] = 1
    moved = utxo_from_info(record_info({**rec, "assets": assets}))
    with pytest.raises(UnsupportedLoanError, match="legacy"):
        resolve_loan(FixtureBackend(utxos=[moved]), rec["out_ref"])


def test_a_new_generation_loan_resolves_with_its_market_and_token():
    deployment = default_deployment()
    rec, record = next(
        (rec, record)
        for rec, record in parsed_records("loan", live=True)
        if record.generation == "new"
    )
    loan = resolve_loan(_loan_backend(), rec["out_ref"])
    assert isinstance(loan, LoanRecord)
    assert isinstance(loan.datum, LoanDatum)
    assert loan.datum == record.datum
    assert loan.market == deployment.market(record.market_id)
    assert loan.utxo == utxo_from_info(record_info(rec))
    token = next(
        u for u in rec["assets"] if u.startswith(loan.market.new_borrow_policy)
    )
    assert loan.token_name == token[56:]


def test_an_output_that_is_not_a_loan_is_refused():
    rec = records_of("market_state", live=True)[0]
    with pytest.raises(UnsupportedLoanError) as info:
        resolve_loan(_loan_backend(), rec["out_ref"])
    assert info.value.out_ref == rec["out_ref"]


def test_a_spent_loan_resolves_only_when_allowed():
    rec, record = next(
        (rec, record)
        for rec, record in parsed_records("loan", live=True)
        if record.generation == "new"
    )
    utxo = utxo_from_info(record_info(rec))
    backend = FixtureBackend(spent=[utxo])
    with pytest.raises(ValueError, match="no tx_out"):
        resolve_loan(backend, rec["out_ref"])
    assert resolve_loan(backend, rec["out_ref"], allow_spent=True).datum == record.datum


# --- resolve_feeds -----------------------------------------------------------


def _feed_info(asset, *, ts_ms, index, price=(7, 10)):
    """A synthetic exact-asset oracle feed at the first oracle validator."""
    validator = default_deployment().oracle_validators[0].script_hash
    policy, name = policy_of(asset), asset[56:]
    datum = [
        b"\x01" * 28,
        cbor2.CBORTag(122, [[bytes.fromhex(policy), bytes.fromhex(name)]]),
        list(price),
        ts_ms,
        900_000,
    ]
    from charli3_dendrite.lending.units import script_payment_address

    return PoolStateInfo(
        address=script_payment_address(validator),
        tx_hash="cd" * 32,
        tx_index=index,
        block_time=1_700_000_000,
        block_index=0,
        block_hash="",
        datum_hash="",
        datum_cbor=cbor2.dumps(datum).hex(),
        assets=Assets(root={"lovelace": 1_500_000, ORACLE_TOKEN_POLICY: 1}),
        plutus_v2=True,
    )


def test_feeds_prefer_the_exact_asset_over_its_policy():
    member = POLICY_WIDE + "4e4d"
    exact = _feed_info(member, ts_ms=1_000, index=0)
    backend = FixtureBackend(records=[*live_records("oracle"), exact])
    feeds = resolve_feeds(backend, [member, "lovelace"])
    assert feeds[member].out_ref == f"{'cd' * 32}#0"
    assert feeds[member].policy_wide is False
    assert feeds["lovelace"].asset_unit == "lovelace"
    assert feeds["lovelace"].policy_wide is False


def test_feeds_fall_back_to_the_policy_wide_feed():
    member = POLICY_WIDE + "4e4d"
    backend = FixtureBackend(records=live_records("oracle"))
    feeds = resolve_feeds(backend, [member])
    assert feeds[member].policy_wide is True
    assert feeds[member].asset_unit == POLICY_WIDE


def test_a_missing_feed_is_refused():
    backend = FixtureBackend(records=live_records("oracle"))
    unknown = "ee" * 28 + "4e4d"
    with pytest.raises(MissingFeedError) as info:
        resolve_feeds(backend, ["lovelace", unknown])
    assert info.value.asset == unknown


def test_feeds_take_the_freshest_exact_feed():
    member = POLICY_WIDE + "4e4d"
    old = _feed_info(member, ts_ms=1_000, index=0)
    new = _feed_info(member, ts_ms=2_000, index=1)
    backend = FixtureBackend(records=[old, new])
    assert resolve_feeds(backend, [member])[member].out_ref == f"{'cd' * 32}#1"


# --- resolve_health_inputs ---------------------------------------------------


def _ada_records():
    ada = default_deployment().market_by_name("Ada")
    return resolve_market_records(FixtureBackend(records=live_records()), [ada])["Ada"]


def test_health_inputs_carry_each_collateral_market_and_feed():
    deployment = default_deployment()
    borrow = _ada_records()
    djed = deployment.market_by_name("DJED")
    units = [djed.qtoken_policy, borrow.market.qtoken_policy]
    inputs = resolve_health_inputs(
        FixtureBackend(records=live_records()),
        units,
        borrow=borrow,
    )
    assert isinstance(inputs, HealthInputs)
    assert sorted(inputs.cparams) == sorted(units)
    assert (
        inputs.cparams[djed.qtoken_policy]
        == borrow.collateral_params[djed.qtoken_policy]
    )
    assert sorted(inputs.states) == sorted({borrow.market.market_id, djed.market_id})
    assert inputs.states[borrow.market.market_id] == borrow.state
    assert inputs.states[djed.market_id].kind is LiqwidKind.MARKET_STATE
    assert sorted(inputs.feeds) == sorted({"lovelace", djed.underlying})


def test_a_wash_borrow_carries_one_state_and_one_feed():
    borrow = _ada_records()
    inputs = resolve_health_inputs(
        FixtureBackend(records=live_records()),
        [borrow.market.qtoken_policy],
        borrow=borrow,
    )
    assert list(inputs.states) == [borrow.market.market_id]
    assert list(inputs.feeds) == ["lovelace"]


@pytest.mark.parametrize("unit", [POLICY_WIDE, POLICY_WIDE + "4e4d", "ee" * 28])
def test_a_collateral_without_an_exact_record_is_refused(unit):
    with pytest.raises(UnsupportedCollateralError) as info:
        resolve_health_inputs(
            FixtureBackend(records=live_records()),
            [unit],
            borrow=_ada_records(),
        )
    assert info.value.unit == unit


def _unlinked(record):
    """``record`` with its CollateralParams datum naming no collateral market."""
    datum = dataclasses.replace(record.datum, underlying="", collateral_market_state="")
    return dataclasses.replace(record, datum=datum)


def test_a_record_naming_no_collateral_market_is_refused():
    borrow = _ada_records()
    q_ada = borrow.market.qtoken_policy
    unlinked = {q_ada: _unlinked(borrow.collateral_params[q_ada])}
    bare = dataclasses.replace(borrow, collateral_params=unlinked)
    with pytest.raises(UnsupportedCollateralError) as info:
        resolve_health_inputs(
            FixtureBackend(records=live_records()),
            [q_ada],
            borrow=bare,
        )
    assert info.value.unit == q_ada


def test_the_collateral_link_is_the_qtoken_market_on_every_live_record():
    deployment = default_deployment()
    exact = [
        record
        for _, record in parsed_records("collateral_params", live=True)
        if not record.datum.policy_wide
    ]
    assert len(exact) == 336
    for record in exact:
        datum = record.datum
        market, linked = collateral_link(datum.collateral, record, deployment)
        assert linked is datum
        assert market == deployment.market_of_qtoken(policy_of(datum.collateral))
        assert market.market_id == policy_of(datum.collateral_market_state)
        assert linked.underlying == market.underlying


@pytest.mark.parametrize(
    "record",
    [
        None,
        lambda: _unlinked(_ada_records().collateral_params[_q_ada()]),
        lambda: parsed_records("market_state", live=True)[0][1],
    ],
    ids=["missing", "unlinked", "another-kind"],
)
def test_the_collateral_link_refuses_a_record_it_cannot_follow(record):
    unit = _q_ada()
    with pytest.raises(UnsupportedCollateralError) as info:
        collateral_link(unit, record() if record else None, default_deployment())
    assert info.value.unit == unit


def test_the_collateral_link_refuses_a_market_outside_the_deployment():
    unit = _q_ada()
    record = _ada_records().collateral_params[unit]
    foreign = dataclasses.replace(record.datum, collateral_market_state="ee" * 28)
    with pytest.raises(UnsupportedCollateralError):
        collateral_link(
            unit,
            dataclasses.replace(record, datum=foreign),
            default_deployment(),
        )


def _q_ada():
    return default_deployment().market_by_name("Ada").qtoken_policy


# --- resolve_now_ms ----------------------------------------------------------


def test_now_is_the_chain_tip():
    backend = FixtureBackend(tip_time=1_790_000_123)
    assert resolve_now_ms(backend) == 1_790_000_123_000


# --- selections over records in hand -----------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        lambda: _market_and_params("POL"),
        lambda: _without_loan_policy("Ada"),
        lambda: _without_staking_pair("Ada"),
    ],
    ids=["short-layout", "no-loan-policy", "no-staking-pair"],
)
def test_the_owner_loan_scripts_of_an_unfit_market_are_refused_unread(case):
    market, params = case()
    with pytest.raises(UnsupportedMarketError) as info:
        resolve.resolve_loan_script_refs(_UnreadableBackend(), market, params)
    assert info.value.market == market.name
    with pytest.raises(UnsupportedMarketError):
        resolve.loan_script_refs(_reference_utxos(), market, params)


def test_the_owner_loan_scripts_are_the_three_of_the_full_set(deployment_hashes):
    for market, params in _live36():
        full = resolve_script_refs(_script_backend(), market, params)
        owner = resolve.resolve_loan_script_refs(_script_backend(), market, params)
        assert isinstance(owner, resolve.LoanScriptRefs)
        assert (owner.loan_validator, owner.loan_policy, owner.borrow_staking) == (
            full.loan_validator,
            full.loan_policy,
            full.borrow_staking,
        ), market.name


def test_the_owner_loan_scripts_are_picked_by_hash_from_utxos_in_hand():
    from charli3_dendrite.lending.transactions.utxos import script_hash_of
    from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
    from tests.lending.liqwid.transactions.replay import fixture

    refs = [utxo_from_dict(u) for u in fixture("collateral_add")["ref_inputs"]]
    market, params = _market_and_params("USDC")
    owner = resolve.loan_script_refs(refs, market, params)
    roles = staking_roles(market, params)
    assert [
        script_hash_of(owner.loan_validator),
        script_hash_of(owner.loan_policy),
        script_hash_of(owner.borrow_staking),
    ] == [params.loan_validator, market.new_borrow_policy, roles.borrow]
    with pytest.raises(ValueError, match="no reference script"):
        resolve.loan_script_refs(
            [u for u in refs if u is not owner.loan_policy], market, params
        )


def test_market_records_select_from_records_in_hand():
    ada = default_deployment().market_by_name("Ada")
    records = [record for _, record in parsed_records("market_params", live=True)]
    records += [record for _, record in parsed_records("market_state", live=True)]
    records += [record for _, record in parsed_records("collateral_params", live=True)]
    assert resolve.market_records(records, ada) == _ada_records()
    with pytest.raises(UnsupportedMarketError):
        resolve.market_records(
            [r for r in records if r.kind is not LiqwidKind.MARKET_STATE],
            ada,
        )


def test_a_loan_record_is_read_from_a_utxo_in_hand():
    rec, record = next(
        (rec, record)
        for rec, record in parsed_records("loan", live=True)
        if record.generation == "new"
    )
    utxo = utxo_from_info(record_info(rec))
    loan = resolve.loan_record(utxo)
    assert loan == resolve_loan(_loan_backend(), rec["out_ref"])
    tokens = loan.market.loan_token_policies
    assert loan.collateral == {
        policy + name: qty for policy, name, qty in utxo.assets if policy not in tokens
    }
    assert all(policy_of(unit) not in tokens for unit in loan.collateral)
    state = utxo_from_info(record_info(records_of("market_state", live=True)[0]))
    with pytest.raises(UnsupportedLoanError):
        resolve.loan_record(state)


def test_feeds_for_selects_among_feeds_in_hand():
    member = POLICY_WIDE + "4e4d"
    feeds = [
        feed
        for info in live_records("oracle")
        if (feed := resolve.parse_feed(info)) is not None
    ]
    found = resolve.feeds_for([member, "lovelace"], feeds)
    assert found == resolve_feeds(
        FixtureBackend(records=live_records("oracle")),
        [member, "lovelace"],
    )
    with pytest.raises(MissingFeedError):
        resolve.feeds_for(["ee" * 28 + "4e4d"], feeds)


def test_health_inputs_select_from_records_in_hand():
    deployment = default_deployment()
    borrow = _ada_records()
    djed = deployment.market_by_name("DJED")
    units = [djed.qtoken_policy, borrow.market.qtoken_policy]
    states = [record for _, record in parsed_records("market_state", live=True)]
    feeds = [
        feed
        for info in live_records("oracle")
        if (feed := resolve.parse_feed(info)) is not None
    ]
    found = resolve.health_inputs(units, borrow=borrow, records=states, feeds=feeds)
    assert found == resolve_health_inputs(
        FixtureBackend(records=live_records()),
        units,
        borrow=borrow,
    )
    with pytest.raises(UnsupportedMarketError):
        resolve.health_inputs(units, borrow=borrow, records=[], feeds=feeds)
