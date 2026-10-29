"""``get_datum_from_address`` honours its ``asset`` filter on every backend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from charli3_dendrite.backend.blockfrost import BlockFrostBackend
from charli3_dendrite.backend.dbsync import DbsyncBackend
from pycardano import Address

ADDRESS = (
    "addr1z8afjx7zl8zzqmnj6ufmcdy85uh8jqg90j4m35myh6lwlrectd0zw3ydwgtk9hpse3mgyez"
    "edms0y054fa47vfkt08qsdxsvyq"
)
POLICY = "bc2d3b7cf1009c788b1daf2208a38026ab20d7f8209d896ad1a09c14"
NAME = "72f9dae79bb2dfa39bd8fadf089ea0c8377b5eaa3caef0549396668aef1f9187"
UNIT = POLICY + NAME
DATUM = "d8799f1903e81a0007a120ff"


def _bf_utxo(tx_hash: str, units: list[str]) -> SimpleNamespace:
    amount = [SimpleNamespace(unit="lovelace", quantity="2000000")]
    amount += [SimpleNamespace(unit=u, quantity="1") for u in units]
    return SimpleNamespace(
        tx_hash=tx_hash,
        output_index=0,
        address=ADDRESS,
        amount=amount,
        data_hash="00" * 32,
        inline_datum=DATUM,
    )


class _FakeBlockfrostApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def address_utxos(self, address: object, **kwargs: object) -> list:
        self.calls.append(("address_utxos", str(address)))
        return [_bf_utxo("aa" * 32, []), _bf_utxo("bb" * 32, [UNIT])]

    def address_utxos_asset(
        self,
        address: object,
        asset: str,
        **kwargs: object,
    ) -> list:
        self.calls.append(("address_utxos_asset", str(address), asset))
        return [_bf_utxo("bb" * 32, [UNIT])]


def _blockfrost() -> tuple[BlockFrostBackend, _FakeBlockfrostApi]:
    backend = object.__new__(BlockFrostBackend)
    api = _FakeBlockfrostApi()
    backend.api = api
    return backend, api


def test_blockfrost_asset_lookup_queries_the_asset_at_the_address() -> None:
    """Blockfrost asks for the address's UTxOs holding the asset."""
    backend, api = _blockfrost()

    ref = backend.get_datum_from_address(Address.decode(ADDRESS), asset=UNIT)

    assert ref is not None
    assert ref.tx_hash == "bb" * 32
    assert ref.datum_cbor == DATUM
    assert api.calls == [("address_utxos_asset", ADDRESS, UNIT)]


def test_blockfrost_without_asset_scans_the_address() -> None:
    """Blockfrost without an asset lists the address's UTxOs."""
    backend, api = _blockfrost()

    ref = backend.get_datum_from_address(Address.decode(ADDRESS))

    assert ref is not None
    assert ref.tx_hash == "aa" * 32
    assert api.calls == [("address_utxos", ADDRESS)]


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def test_dbsync_asset_lookup_starts_from_the_asset() -> None:
    """db-sync narrows the outputs by the asset's indexes."""
    backend = object.__new__(DbsyncBackend)

    query, kwargs = backend._get_datum_from_address(
        address=Address.decode(ADDRESS),
        asset=UNIT,
    )

    sql = _normalized(query)
    assert kwargs["policy"] == bytes.fromhex(POLICY)
    assert kwargs["name"] == bytes.fromhex(NAME)
    # The unique (policy, name) index on multi_asset and the ident index on
    # ma_tx_out bound the candidate outputs to those holding the asset.
    assert (
        "tx_out.id IN ( SELECT mto.tx_out_id FROM ma_tx_out mto "
        "JOIN multi_asset ma ON ma.id = mto.ident "
        "WHERE ma.policy = %(policy)b AND ma.name = %(name)b )"
    ) in sql
    # The outer query no longer fans out over every multi-asset row.
    assert "LEFT JOIN ma_tx_out" not in sql


def test_dbsync_without_asset_has_no_asset_filter() -> None:
    """db-sync without an asset filters by address only."""
    backend = object.__new__(DbsyncBackend)

    query, kwargs = backend._get_datum_from_address(address=Address.decode(ADDRESS))

    assert "policy" not in kwargs
    assert "%(policy)b" not in query


def test_dbsync_no_match_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """db-sync returns None when no output matches."""
    backend = object.__new__(DbsyncBackend)
    monkeypatch.setattr(backend, "db_query", lambda *_: [])

    assert backend.get_datum_from_address(Address.decode(ADDRESS), asset=UNIT) is None
