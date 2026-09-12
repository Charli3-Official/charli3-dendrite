"""Backend redeemer reads: the default hook and the concrete backends."""

from __future__ import annotations

import os

import pytest

from charli3_dendrite.backend.backend_base import AbstractBackend
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
