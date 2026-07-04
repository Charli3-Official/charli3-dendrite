"""Gated live snapshot over real dbsync (FLUID_E2E=1).

Builds a dbsync-backed backend, runs a full FluidTokens snapshot, and asserts the
deployment yields at least one pool and one loan with positive accrued debt. Skipped
unless `FLUID_E2E=1` (needs reachable dbsync creds from the environment / `.env`).
"""

import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.environ.get("FLUID_E2E") != "1",
    reason="requires live dbsync (FLUID_E2E=1)",
)


def _backend():
    load_dotenv()
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def test_snapshot_live():
    from charli3_dendrite.lending.fluidtokens.loader import snapshot

    book = snapshot(_backend())

    assert book.pool is not None, "no pools discovered on-chain"
    active = book.active_loans()
    assert active, "no loans parsed on-chain"
    assert any(ln.current_debt() > 0 for ln in active)
