"""The captured Liqwid V2 transaction fixtures cover every capture job."""

from __future__ import annotations

from tests.lending.liqwid.transactions.fixtures._capture_txs import JOBS
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import labels


def test_every_job_was_captured() -> None:
    assert set(labels()) == set(JOBS)
    for label, tx_hash in JOBS.items():
        fix = fixture(label)
        assert fix["tx_hash"] == tx_hash
        assert fix["inputs"], label
        assert fix["redeemers"], label
