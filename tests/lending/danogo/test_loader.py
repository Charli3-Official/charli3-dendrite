import pytest
from pycardano import Address

from charli3_dendrite.lending.base import LendingBook
from charli3_dendrite.lending.danogo.constants import resolve_addresses
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.loader import build_book
from charli3_dendrite.lending.oracles.models import PriceMap


class _FakeRow:
    def __init__(self, cbor):
        self.datum_cbor = cbor


class _FakeBackend:
    def __init__(self, cbor):
        self._cbor = cbor

    def get_pool_utxos(
        self, addresses, assets=None, limit=1000, page=0, historical=True
    ):
        return [_FakeRow(self._cbor)]


def test_resolve_addresses_reads_protocol_datum():
    d = ProtocolDatum(
        pool_skh=bytes.fromhex("11" * 28),
        loan_skh=bytes.fromhex("22" * 28),
        config_pool_skh=bytes.fromhex("33" * 28),
        oracle_skh=bytes.fromhex("44" * 28),
    )
    backend = _FakeBackend(d.to_cbor_hex())
    addrs = resolve_addresses(backend, config_nft="aa" * 28 + "00")
    assert addrs["pool"].startswith("addr1")
    assert addrs["loan"].startswith("addr1")
    assert set(addrs) == {"pool", "loan", "config_pool", "oracle"}
    assert Address.decode(addrs["pool"]).payment_part.payload == bytes.fromhex(
        "11" * 28
    )
    assert Address.decode(addrs["loan"]).payment_part.payload == bytes.fromhex(
        "22" * 28
    )


def test_resolve_addresses_raises_without_anchor(monkeypatch):
    from charli3_dendrite.lending.danogo import constants

    monkeypatch.setattr(constants, "PROTOCOL_CONFIG_NFT", None)
    backend = _FakeBackend("00")
    with pytest.raises(ValueError):
        resolve_addresses(backend)


def test_build_book_stitches_and_filters():
    from tests.lending.danogo.test_state import _loan_state
    from charli3_dendrite.lending.oracles.models import OraclePrice
    from charli3_dendrite.lending.oracles.models import OracleSource

    loan = _loan_state()
    pm = PriceMap()
    pm.add(
        OraclePrice(
            token="aa" + "0" * 54,
            quote="lovelace",
            num=1,
            denom=2,
            source=OracleSource.DANOGO_AGGREGATOR,
        )
    )
    book = build_book(pools=[loan._pool], loans=[loan], prices=pm, now_ms=loan._now_ms)
    assert loan in book.liquidatable_loans()  # price 1/2 -> underwater


def test_build_book_empty_inputs():
    book = build_book(pools=[], loans=[], prices=PriceMap(), now_ms=0)
    assert isinstance(book, LendingBook)
    assert book.active_loans() == []
