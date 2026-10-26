"""The shared dbsync resolvers: out-ref and wallet reads, typed scripts, datum hashes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pycardano import PlutusV2Script

from charli3_dendrite.lending.transactions import resolve
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import plutus_script

TX_HASH = "aa" * 32
POLICY = "cafe" * 14


class _FakeBackend:
    """A dbsync stand-in: answers each ``db_query`` with the next canned row list."""

    def __init__(self, rows_by_call: list[list[dict[str, Any]]]) -> None:
        self._rows = rows_by_call
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def db_query(self, sql: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self._rows.pop(0)


def _out_row(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    row: dict[str, Any] = {
        "id": 42,
        "lovelace": 2_000_000,
        "address": "addr1xyz",
        "datum": None,
        "datum_hash": None,
        "ref_script": None,
        "ref_script_type": None,
    }
    row.update(overrides)
    return row


def test_db_query_rows_passes_the_query_through() -> None:
    backend = _FakeBackend([[{"x": 1}]])
    assert resolve.db_query_rows(backend, "SELECT 1", {"a": 2}) == [{"x": 1}]
    assert backend.calls == [("SELECT 1", {"a": 2})]


def test_db_query_rows_names_a_backend_without_db_query() -> None:
    class Blockfrostish:
        pass

    with pytest.raises(TypeError) as err:
        resolve.db_query_rows(Blockfrostish(), "SELECT 1", {})
    assert str(err.value) == (
        "live resolution requires a dbsync-backed backend "
        "(got Blockfrostish, which has no db_query)"
    )


def test_utxo_assets_maps_rows_to_triples() -> None:
    backend = _FakeBackend(
        [[{"policy": POLICY, "name": "", "quantity": "1"}]],
    )
    assert resolve.utxo_assets(backend, 7) == [(POLICY, "", 1)]
    sql, args = backend.calls[0]
    assert "ORDER BY ma.policy, ma.name" in sql
    assert args == {"oid": 7}


def test_resolve_utxo_by_outref_types_a_plutus_v2_reference_script() -> None:
    script = "4e4d01000033222220051200120011"
    backend = _FakeBackend(
        [
            [_out_row(ref_script=script, ref_script_type="plutusV2")],
            [{"policy": POLICY, "name": "", "quantity": 1}],
        ],
    )
    utxo = resolve.resolve_utxo_by_outref(backend, TX_HASH, 3)
    assert utxo.ref_script == script
    assert utxo.ref_script_type == "plutusV2"
    assert isinstance(plutus_script(utxo), PlutusV2Script)
    assert utxo.out_ref == (TX_HASH, 3)
    assert utxo.assets == [(POLICY, "", 1)]
    assert utxo.datum_hash is None
    sql, args = backend.calls[0]
    assert "consumed_by_tx_id IS NULL" in sql
    assert args == {"h": TX_HASH, "i": 3}


def test_resolve_utxo_by_outref_reads_a_datum_hash_without_inline_datum() -> None:
    digest = "dd" * 32
    backend = _FakeBackend([[_out_row(datum_hash=digest)], []])
    utxo = resolve.resolve_utxo_by_outref(backend, TX_HASH, 0)
    assert utxo.datum is None
    assert utxo.datum_hash == digest
    assert "encode(o.data_hash, 'hex')" in backend.calls[0][0]


def test_resolve_utxo_by_outref_prefers_the_inline_datum() -> None:
    backend = _FakeBackend([[_out_row(datum="d87980", datum_hash="dd" * 32)], []])
    utxo = resolve.resolve_utxo_by_outref(backend, TX_HASH, 0)
    assert utxo.datum == "d87980"
    assert utxo.datum_hash is None


def test_resolve_utxo_by_outref_allow_spent_drops_the_unspent_filter() -> None:
    backend = _FakeBackend([[_out_row()], []])
    resolve.resolve_utxo_by_outref(backend, TX_HASH, 0, allow_spent=True)
    assert "consumed_by_tx_id IS NULL" not in backend.calls[0][0]


def test_resolve_utxo_by_outref_raises_on_no_row() -> None:
    backend = _FakeBackend([[]])
    with pytest.raises(ValueError, match=rf"no tx_out for out-ref \({TX_HASH}, 5\)"):
        resolve.resolve_utxo_by_outref(backend, TX_HASH, 5)


def test_resolve_utxo_by_outref_needs_db_query() -> None:
    with pytest.raises(TypeError, match="got object, which has no db_query"):
        resolve.resolve_utxo_by_outref(object(), TX_HASH, 0)


def test_resolve_funding_resolves_each_unspent_output_newest_first() -> None:
    backend = _FakeBackend(
        [
            [{"tx_hash": "bb" * 32, "idx": 1}, {"tx_hash": "cc" * 32, "idx": 0}],
            [_out_row(lovelace=5_000_000)],
            [],
            [_out_row(lovelace=3_000_000)],
            [],
        ],
    )
    funding = resolve.resolve_funding(backend, "addr1wallet", limit=2)
    assert [u.out_ref for u in funding] == [("bb" * 32, 1), ("cc" * 32, 0)]
    assert [u.lovelace for u in funding] == [5_000_000, 3_000_000]
    assert all(isinstance(u, Utxo) for u in funding)
    sql, args = backend.calls[0]
    assert "consumed_by_tx_id IS NULL" in sql
    assert "ORDER BY o.id DESC" in sql
    assert args == {"addr": "addr1wallet", "lim": 2}
    # each funding output is read unspent
    assert all("consumed_by_tx_id IS NULL" in s for s, _ in backend.calls[1::2])


def test_fluidtokens_resolve_reexports_the_seam_objects() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions import resolve as ft

    assert ft.db_query_rows is resolve.db_query_rows
    assert ft.utxo_assets is resolve.utxo_assets
    assert ft.resolve_utxo_by_outref is resolve.resolve_utxo_by_outref
    assert ft.resolve_funding is resolve.resolve_funding


def test_seam_resolve_imports_no_protocol_package() -> None:
    source = Path(resolve.__file__ or "").read_text()
    assert "lending.fluidtokens" not in source
    assert "lending.danogo" not in source
    assert "lending.liqwid" not in source
