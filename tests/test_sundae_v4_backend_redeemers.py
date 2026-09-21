"""Backend redeemer reads: the default hook and the concrete backends."""

from __future__ import annotations

import os

import pytest

from charli3_dendrite.backend.backend_base import AbstractBackend
from charli3_dendrite.backend.blockfrost import BlockFrostBackend
from charli3_dendrite.backend.dbsync import DbsyncBackend
from charli3_dendrite.backend.ogmios_kupo import OgmiosKupoBackend
from charli3_dendrite.dataclasses.models import RedeemerRecord


class _Minimal(AbstractBackend):
    """A backend that implements nothing beyond the abstract surface."""

    def get_pool_utxos(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_pool_in_tx(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def last_block(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_pool_utxos_in_block(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_script_from_address(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_stake_rewards(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_historical_order_utxos(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_order_utxos_by_block_or_tx(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_cancel_utxos(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_datum_from_address(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def get_axo_target(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError


def test_get_redeemers_is_not_abstract_but_unsupported_by_default() -> None:
    backend = _Minimal()
    with pytest.raises(NotImplementedError):
        backend.get_redeemers("00" * 32)


def test_redeemer_record_round_trips_by_field_name() -> None:
    record = RedeemerRecord(
        tx_hash="ab" * 32,
        purpose="reward",
        index=0,
        script_hash="cd" * 28,
        data_cbor="d87980",
    )
    assert record.model_dump(by_alias=False) == {
        "tx_hash": "ab" * 32,
        "purpose": "reward",
        "index": 0,
        "script_hash": "cd" * 28,
        "data_cbor": "d87980",
    }


def test_ogmios_kupo_cannot_read_redeemers() -> None:
    backend = OgmiosKupoBackend.__new__(OgmiosKupoBackend)
    with pytest.raises(NotImplementedError):
        backend.get_redeemers("00" * 32)


def test_dbsync_get_redeemers_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = DbsyncBackend.__new__(DbsyncBackend)
    rows = [
        {
            "tx_hash": "ab" * 32,
            "purpose": "reward",
            "index": 1,
            "script_hash": "cd" * 28,
            "data_cbor": "d87980",
        },
        {
            "tx_hash": "ab" * 32,
            "purpose": "spend",
            "index": 0,
            "script_hash": "ef" * 28,
            "data_cbor": "d87a80",
        },
    ]
    captured: dict = {}

    def fake_query(query: str, args: dict | None = None) -> list[dict]:
        captured["query"] = query
        captured["args"] = args
        return rows

    monkeypatch.setattr(backend, "db_query", fake_query)
    records = backend.get_redeemers("ab" * 32)
    assert [r.model_dump(by_alias=False) for r in records] == rows
    assert captured["args"] == {"tx_hash": bytes.fromhex("ab" * 32)}
    assert "redeemer_data" in captured["query"]


def test_dbsync_get_redeemers_coalesces_a_null_script_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """db-sync's ``script_hash`` is nullable; the query coalesces it to ``''``."""
    backend = DbsyncBackend.__new__(DbsyncBackend)
    captured: dict = {}

    def fake_query(query: str, _args: dict | None = None) -> list[dict]:
        captured["query"] = query
        return []

    monkeypatch.setattr(backend, "db_query", fake_query)
    backend.get_redeemers("ab" * 32)
    assert "COALESCE(encode(r.script_hash, 'hex'), '')" in captured["query"]


def test_blockfrost_get_redeemers_fetches_each_datum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Item:
        def __init__(self, i: int) -> None:
            self.tx_index = i
            self.purpose = "reward"
            self.script_hash = "cd" * 28
            self.redeemer_data_hash = f"{i:064x}"

    class _Api:
        def transaction_redeemers(self, tx_hash: str, **_: object) -> list[_Item]:
            assert tx_hash == "ab" * 32
            return [_Item(0), _Item(1)]

        def script_datum_cbor(self, datum_hash: str, **_: object) -> object:
            return type("R", (), {"cbor": "d8798" + datum_hash[-1]})()

    backend = BlockFrostBackend.__new__(BlockFrostBackend)
    backend.api = _Api()
    records = backend.get_redeemers("ab" * 32)
    assert [(r.purpose, r.index, r.data_cbor) for r in records] == [
        ("reward", 0, "d87980"),
        ("reward", 1, "d87981"),
    ]


def test_blockfrost_get_redeemers_coalesces_a_none_script_hash() -> None:
    """A redeemer the API reports with no script comes back with ``""``."""

    class _Item:
        def __init__(self) -> None:
            self.tx_index = 0
            self.purpose = "reward"
            self.script_hash = None
            self.redeemer_data_hash = "0" * 64

    class _Api:
        def transaction_redeemers(self, tx_hash: str, **_: object) -> list[_Item]:
            return [_Item()]

        def script_datum_cbor(self, datum_hash: str, **_: object) -> object:
            return type("R", (), {"cbor": "d87980"})()

    backend = BlockFrostBackend.__new__(BlockFrostBackend)
    backend.api = _Api()
    records = backend.get_redeemers("ab" * 32)
    assert records[0].script_hash == ""


@pytest.mark.skipif(
    not os.environ.get("DBSYNC_HOST"), reason="needs a configured db-sync backend"
)
def test_dbsync_get_redeemers_live_matches_the_recorded_create_redeemers() -> None:
    import json
    from pathlib import Path

    from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment

    fix = json.loads(
        (Path(__file__).parent / "sundae_v4_auditfinal_fixtures.json").read_text()
    )
    rec = next(r for r in fix["pool_datums"] if r.get("module_create_redeemers"))
    deployment = SundaeV4Deployment.for_network(rec["network"])
    records = DbsyncBackend().get_redeemers(rec["tx"])
    by_script = {r.script_hash: r.data_cbor for r in records if r.purpose == "reward"}
    for kind, cbor in rec["module_create_redeemers"].items():
        assert by_script[deployment.validator(f"{kind}.withdraw").hex()] == cbor
