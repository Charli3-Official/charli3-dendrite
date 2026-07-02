"""Unit: resolve_utxo_by_asset issues the expected dbsync query and maps the row."""
from __future__ import annotations

import pytest


class _FakeBackend:
    def __init__(self, rows_by_call: list[list[dict]]) -> None:
        self._rows = rows_by_call
        self.calls: list[tuple[str, dict]] = []

    def db_query(self, sql: str, args: dict) -> list[dict]:
        self.calls.append((sql, args))
        return self._rows.pop(0)


def test_resolve_utxo_by_asset_returns_newest_and_maps_row() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
        resolve_utxo_by_asset,
    )

    backend = _FakeBackend(
        [
            [{"tx_hash": "aa" * 32, "idx": 3}],  # by-asset lookup
            [  # resolve_utxo_by_outref
                {
                    "id": 42,
                    "lovelace": 2_000_000,
                    "address": "addr1xyz",
                    "datum": None,
                    "ref_script": None,
                    "ref_script_type": None,
                }
            ],
            [],  # _assets
        ]
    )
    utxo = resolve_utxo_by_asset(backend, "cafe", "beef")
    assert utxo.out_ref == ("aa" * 32, 3)
    assert utxo.lovelace == 2_000_000
    # first query filters unspent by default and binds policy+name
    first_sql, first_args = backend.calls[0]
    assert "consumed_by_tx_id IS NULL" in first_sql
    assert first_args == {"p": "cafe", "n": "beef"}


def test_resolve_utxo_by_asset_allow_spent_drops_unspent_filter() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
        resolve_utxo_by_asset,
    )

    backend = _FakeBackend(
        [
            [{"tx_hash": "bb" * 32, "idx": 0}],
            [
                {
                    "id": 7,
                    "lovelace": 1_500_000,
                    "address": "addr1abc",
                    "datum": None,
                    "ref_script": None,
                    "ref_script_type": None,
                }
            ],
            [],
        ]
    )
    resolve_utxo_by_asset(backend, "cafe", "beef", allow_spent=True)
    first_sql, _ = backend.calls[0]
    assert "consumed_by_tx_id IS NULL" not in first_sql


def test_resolve_utxo_by_asset_raises_when_no_match() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
        resolve_utxo_by_asset,
    )

    backend = _FakeBackend([[]])
    with pytest.raises(ValueError, match="no UTxO holding asset cafebeef"):
        resolve_utxo_by_asset(backend, "cafe", "beef")
