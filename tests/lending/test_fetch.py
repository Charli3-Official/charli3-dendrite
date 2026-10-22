"""The shared paged unspent-UTxO fetch and evaluation-time default (offline)."""

import time
from types import SimpleNamespace

import pytest

from charli3_dendrite.lending import fetch
from charli3_dendrite.lending.fetch import PAGE_SIZE
from charli3_dendrite.lending.fetch import resolve_now_ms
from charli3_dendrite.lending.fetch import unspent_utxos
from charli3_dendrite.lending.fluidtokens_v4 import loader as ft_loader
from charli3_dendrite.lending.fluidtokens_v4 import market as ft_market
from charli3_dendrite.lending.liqwid import loader as liqwid_loader

ADDRESS = "addr_test"


def _row(tx_hash, tx_index):
    return SimpleNamespace(tx_hash=tx_hash, tx_index=tx_index)


class Backend:
    """Serves ``rows`` with LIMIT / OFFSET paging; records every call."""

    def __init__(self, rows, *, page_blind=False):
        self.rows = rows
        self.page_blind = page_blind
        self.calls = []

    def get_pool_utxos(self, addresses, limit=1000, page=0, historical=True):
        self.calls.append((tuple(addresses), limit, page, historical))
        assert len(self.calls) < 10, "kept paging"
        start = 0 if self.page_blind else page * limit
        return iter(self.rows[start : start + limit])


def test_page_size_is_shared_by_both_loaders():
    assert PAGE_SIZE == 1000
    assert ft_loader.PAGE_SIZE is PAGE_SIZE
    assert liqwid_loader.PAGE_SIZE is PAGE_SIZE


def test_pages_until_a_short_page():
    rows = [_row("a", i) for i in range(25)]
    backend = Backend(rows)
    assert unspent_utxos(backend, ADDRESS, page_size=10) == rows
    assert backend.calls == [((ADDRESS,), 10, page, False) for page in range(3)]


def test_a_full_last_page_costs_one_empty_page():
    rows = [_row("a", i) for i in range(20)]
    backend = Backend(rows)
    assert unspent_utxos(backend, ADDRESS, page_size=10) == rows
    assert [page for _, _, page, _ in backend.calls] == [0, 1, 2]


def test_defaults_to_one_page_of_page_size():
    backend = Backend([_row("a", 0)])
    assert unspent_utxos(backend, ADDRESS) == [backend.rows[0]]
    assert backend.calls == [((ADDRESS,), PAGE_SIZE, 0, False)]


def test_stops_when_the_backend_ignores_the_page():
    rows = [_row("a", i) for i in range(25)]
    backend = Backend(rows, page_blind=True)
    assert unspent_utxos(backend, ADDRESS, page_size=10) == rows[:10]
    assert len(backend.calls) == 2


def test_keeps_the_first_row_of_each_out_ref():
    first = _row("a", 0)
    again = _row("a", 0)
    rows = [first, _row("b", 0), again, _row("a", 1)]
    found = unspent_utxos(Backend(rows), ADDRESS, page_size=10)
    assert found == [first, rows[1], rows[3]]
    assert found[0] is first


def test_resolve_now_ms_keeps_a_given_time():
    assert resolve_now_ms(1_790_000_000_000) == 1_790_000_000_000
    assert resolve_now_ms(0) == 0


def test_resolve_now_ms_defaults_to_the_clock():
    before = int(time.time() * 1000)
    now = resolve_now_ms(None)
    assert before <= now <= int(time.time() * 1000)


@pytest.mark.parametrize("module", [ft_loader, liqwid_loader])
def test_both_loaders_fetch_through_the_shared_helper(module, monkeypatch):
    assert module.unspent_utxos is fetch.unspent_utxos
    assert module.resolve_now_ms is fetch.resolve_now_ms
    calls = []

    def spy(backend, address, *, page_size=PAGE_SIZE):
        calls.append((address, page_size))
        return []

    monkeypatch.setattr(module, "unspent_utxos", spy)
    if module is ft_loader:
        selector = SimpleNamespace(kind=None, address=ADDRESS)
        assert module.fetch_entities(None, selector, page_size=7) == []
    else:
        selector = SimpleNamespace(kind=None, market_id=None, address=ADDRESS)
        assert module.fetch_records(None, [selector], page_size=7) == []
    assert calls == [(ADDRESS, 7)]


def test_no_loader_keeps_its_own_paging_loop():
    assert not hasattr(liqwid_loader, "_unspent")
    assert not hasattr(ft_loader, "time")
    assert not hasattr(liqwid_loader, "time")
    assert not hasattr(ft_market, "time")
