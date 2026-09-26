"""FluidTokens V4 snapshot over a fake backend serving captured UTxOs (offline)."""

from pycardano import Address

from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.loader import fetch_entities
from charli3_dendrite.lending.fluidtokens_v4.loader import snapshot
from charli3_dendrite.lending.oracles.models import PriceMap
from tests.lending.fluidtokens_v4.records import FIX
from tests.lending.fluidtokens_v4.records import record_info

KINDS = ("pool", "pool_manager", "loan", "asset_manager", "lender_manager")
NOW_MS = 1_790_000_000_000


def _credential(address):
    return Address.decode(address).payment_part.payload.hex()


class FakeBackend:
    """Serves captured UTxOs by payment credential, paged like dbsync."""

    def __init__(self, records=None):
        self.by_credential = {}
        for kind in KINDS:
            for rec in (records or FIX)[kind]:
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
        assert len(self.calls) < self.MAX_CALLS, "fetch_entities kept paging"
        return super().get_pool_utxos(addresses, assets, limit, 0, historical)


def test_snapshot_parses_every_captured_kind():
    snap = snapshot(FakeBackend(), now_ms=NOW_MS)
    assert len(snap.pools) == len(FIX["pool"])
    assert len(snap.pool_managers) == len(FIX["pool_manager"])
    assert len(snap.loans) == len(FIX["loan"])
    assert len(snap.asset_managers) == len(FIX["asset_manager"])
    assert len(snap.lender_managers) == len(FIX["lender_manager"])
    assert snap.requests == []
    assert snap.locked_borrower_managers == []


def test_snapshot_keeps_loans_whose_pool_is_gone():
    snap_all = snapshot(FakeBackend(), now_ms=NOW_MS)
    linked = next(loan for loan in snap_all.loans if loan._pool is not None)
    records = dict(FIX)
    records["pool"] = [p for p in FIX["pool"] if p["out_ref"] != linked._pool.out_ref]
    snap = snapshot(FakeBackend(records), now_ms=NOW_MS)
    orphan = next(loan for loan in snap.loans if loan.out_ref == linked.out_ref)
    assert len(snap.loans) == len(snap_all.loans)
    assert orphan._pool is None
    assert orphan.pool_id == linked.pool_id
    assert orphan.current_debt() == linked.current_debt()


def test_every_loan_is_linked_and_timed():
    snap = snapshot(FakeBackend(), now_ms=NOW_MS)
    pool_ids = {pool.pool_id for pool in snap.pools}
    for loan in snap.loans:
        # Installment and recast loans fall below the principal; test_state pins
        # the exact perpetual value.
        assert loan.current_debt() >= 0
        assert (loan._pool is not None) == (loan.pool_id in pool_ids)


def test_book_prices_every_loan():
    snap = snapshot(FakeBackend(), now_ms=NOW_MS)
    book = snap.book(PriceMap())
    assert book.active_loans() == snap.loans
    assert book.liquidatable_loans() == []  # unpriced loans are never flagged


def test_fetch_pages_until_a_short_page():
    backend = FakeBackend()
    selector = entity_selectors()[EntityKind.POOL]
    states = fetch_entities(backend, selector, page_size=10)
    assert len(states) == len(FIX["pool"])
    pages = [page for _, _, page, _ in backend.calls]
    assert pages == list(range(len(FIX["pool"]) // 10 + 1))
    assert all(historical is False for *_, historical in backend.calls)


def test_fetch_stops_when_the_backend_ignores_the_page():
    backend = PageBlindBackend()
    selector = entity_selectors()[EntityKind.POOL]
    states = fetch_entities(backend, selector, page_size=10)
    first_page = sorted(rec["out_ref"] for rec in FIX["pool"][:10])
    assert [s.out_ref for s in states] == first_page
    assert len(backend.calls) == 2


def test_fetch_deduplicates_repeated_rows():
    backend = FakeBackend()
    selector = entity_selectors()[EntityKind.POOL]
    credential = selector.payment_credential
    backend.by_credential[credential] = backend.by_credential[credential] * 2
    states = fetch_entities(backend, selector, page_size=1000)
    assert len(states) == len(FIX["pool"])
    assert [s.out_ref for s in states] == sorted(s.out_ref for s in states)


def test_snapshot_without_dbsync_uses_the_default_config():
    from charli3_dendrite.lending.fluidtokens_v4.constants import default_config

    assert snapshot(FakeBackend(), now_ms=1).config == default_config()
