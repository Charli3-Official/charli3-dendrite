"""Liqwid V2 records and live snapshot over a fake backend serving captured UTxOs (offline)."""

import dataclasses
import time
from collections import Counter

import pytest
from pycardano import Address

from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.loader import PAGE_SIZE
from charli3_dendrite.lending.liqwid.loader import LiqwidSnapshot
from charli3_dendrite.lending.liqwid.loader import fetch_records
from charli3_dendrite.lending.liqwid.loader import snapshot
from charli3_dendrite.lending.markets import ParseContext
from tests.lending.liqwid.records import API
from tests.lending.liqwid.records import UTXO_KINDS
from tests.lending.liqwid.records import live_context
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of

NOW_MS = 1_790_000_000_000
PARAM_KINDS = {LiqwidKind.MARKET_PARAMS, LiqwidKind.COLLATERAL_PARAMS}
# The kinds the read-side routes select; the action kind sits outside them.
ROUTED_KINDS = [LiqwidKind(kind) for kind in UTXO_KINDS]


def _credential(address):
    return Address.decode(address).payment_part.payload.hex()


def _out_ref(info):
    return f"{info.tx_hash}#{info.tx_index}"


class FakeBackend:
    """Serves captured live UTxOs by payment credential, paged like dbsync."""

    def __init__(self, records=None):
        if records is None:
            records = {kind: records_of(kind, live=True) for kind in UTXO_KINDS}
        self.by_credential = {}
        for recs in records.values():
            for rec in recs:
                self.by_credential.setdefault(_credential(rec["address"]), []).append(
                    record_info(rec),
                )
        self.calls = []

    def get_pool_utxos(
        self, addresses, assets=None, limit=1000, page=0, historical=True
    ):
        self.calls.append((tuple(addresses), limit, page, historical))
        rows = [
            info
            for a in addresses
            for info in self.by_credential.get(_credential(a), [])
        ]
        return rows[page * limit : (page + 1) * limit]


class PageBlindBackend(FakeBackend):
    """Ignores ``page``: every call returns the first ``limit`` rows."""

    MAX_CALLS = 5

    def get_pool_utxos(
        self, addresses, assets=None, limit=1000, page=0, historical=True
    ):
        assert len(self.calls) < self.MAX_CALLS, "fetch_records kept paging"
        return super().get_pool_utxos(addresses, assets, limit, 0, historical)


def _param_selectors():
    credential = default_deployment().param_credential
    return [s for s in liqwid_selectors() if s.payment_credential == credential]


def _genuine_out_refs(infos, kinds):
    refs = set()
    for info in infos:
        record = parse_utxo(info)
        if record is not None and record.kind in kinds:
            refs.add(record.out_ref)
    return refs


def _live_genuine(kind):
    """Out-refs of the captured live records of ``kind`` that parse as ``kind``."""
    infos = [record_info(rec) for rec in records_of(kind.value, live=True)]
    return _genuine_out_refs(infos, {kind})


def test_fetch_pages_until_a_short_page():
    backend = FakeBackend()
    selectors = _param_selectors()
    assert len(selectors) > 1  # one per MarketParams and per CollateralParams policy
    assert len({s.address for s in selectors}) == 1
    rows = backend.by_credential[default_deployment().param_credential]
    records = fetch_records(backend, selectors, page_size=10)
    assert [r.out_ref for r in records] == sorted(_genuine_out_refs(rows, PARAM_KINDS))
    assert {r.kind for r in records} == PARAM_KINDS
    pages = [page for _, _, page, _ in backend.calls]
    assert pages == list(range(len(rows) // 10 + 1))
    assert {addresses for addresses, *_ in backend.calls} == {(selectors[0].address,)}
    assert all(historical is False for *_, historical in backend.calls)


def test_fetch_reads_a_shared_address_once():
    backend = FakeBackend()
    selectors = _param_selectors()
    fetch_records(backend, selectors)
    assert backend.calls == [((selectors[0].address,), PAGE_SIZE, 0, False)]


def test_fetch_stops_when_the_backend_ignores_the_page():
    backend = PageBlindBackend()
    selectors = _param_selectors()
    rows = backend.by_credential[default_deployment().param_credential]
    assert len(rows) > 10
    records = fetch_records(backend, selectors, page_size=10)
    first_page = sorted(_genuine_out_refs(rows[:10], PARAM_KINDS))
    assert [r.out_ref for r in records] == first_page
    assert len(backend.calls) == 2


def test_fetch_deduplicates_repeated_rows():
    once = fetch_records(FakeBackend(), _param_selectors())
    backend = FakeBackend()
    credential = default_deployment().param_credential
    backend.by_credential[credential] = backend.by_credential[credential] * 2
    twice = fetch_records(backend, _param_selectors(), page_size=PAGE_SIZE)
    assert [r.out_ref for r in twice] == [r.out_ref for r in once]
    assert len({r.out_ref for r in twice}) == len(twice)


def test_fetch_keeps_only_the_selected_kind_and_market():
    infos = [record_info(rec) for rec in records_of("market_params", live=True)]
    parsed = [parse_utxo(info) for info in infos]
    params = [r for r in parsed if r is not None and r.kind is LiqwidKind.MARKET_PARAMS]
    market_id = params[0].market_id
    selectors = [
        s
        for s in _param_selectors()
        if s.kind is LiqwidKind.MARKET_PARAMS and s.market_id == market_id
    ]
    assert selectors
    records = fetch_records(FakeBackend(), selectors)
    expected = sorted(r.out_ref for r in params if r.market_id == market_id)
    assert [r.out_ref for r in records] == expected
    assert {(r.kind, r.market_id) for r in records} == {
        (LiqwidKind.MARKET_PARAMS, market_id),
    }


def test_fetch_reads_every_selector_address_once():
    backend = FakeBackend()
    selectors = liqwid_selectors()
    fetch_records(backend, selectors)
    first_pages = Counter(
        addresses for addresses, _, page, _ in backend.calls if page == 0
    )
    assert set(first_pages) == {(s.address,) for s in selectors}
    assert set(first_pages.values()) == {1}


def test_snapshot_builds_every_market_and_live_loan():
    snap = snapshot(FakeBackend(), NOW_MS)
    market_ids = [r.market_id for r in snap.market_states]
    assert len(market_ids) == len(set(market_ids)) == len(API["markets"]) == 35
    assert set(market_ids) == set(API["markets"])
    assert [r.out_ref for r in snap.market_states] == sorted(
        _live_genuine(LiqwidKind.MARKET_STATE),
    )
    assert snap.loans
    assert {r.kind for r in snap.loans} == {LiqwidKind.LOAN}
    assert [r.out_ref for r in snap.loans] == sorted(_live_genuine(LiqwidKind.LOAN))
    assert snap.now_ms == NOW_MS


def test_snapshot_context_is_the_live_context():
    snap = snapshot(FakeBackend(), NOW_MS)
    context_kinds = set(ROUTED_KINDS) - {LiqwidKind.LOAN}
    expected = _genuine_out_refs(live_context().utxos, context_kinds)
    got = [_out_ref(info) for info in snap.context.utxos]
    assert got == sorted(expected)
    assert snap.context.slot is None
    assert {parse_utxo(info).kind for info in snap.context.utxos} == context_kinds
    assert {r.out_ref for r in snap.market_states} <= set(got)
    assert not {r.out_ref for r in snap.loans} & set(got)


def test_snapshot_is_frozen_and_defaults_now_to_the_clock():
    before = int(time.time() * 1000)
    snap = snapshot(FakeBackend())
    assert before <= snap.now_ms <= int(time.time() * 1000)
    assert isinstance(snap, LiqwidSnapshot)
    assert isinstance(snap.context, ParseContext)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.now_ms = 0


def test_snapshot_with_the_default_deployment_matches_the_bundled_one():
    implicit = snapshot(FakeBackend(), NOW_MS)
    explicit = snapshot(FakeBackend(), NOW_MS, default_deployment())
    assert explicit == implicit


def test_snapshot_over_an_empty_backend_is_empty():
    snap = snapshot(FakeBackend({}), NOW_MS)
    assert snap.market_states == ()
    assert snap.loans == ()
    assert snap.context == ParseContext(slot=None, utxos=())
