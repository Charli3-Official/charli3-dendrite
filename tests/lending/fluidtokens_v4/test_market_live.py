"""Gated: every live FluidTokens V4 pool, loan and request converts (FLUID_E2E=1).

Read-only over real dbsync; skipped unless ``FLUID_E2E=1``.
"""

import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.environ.get("FLUID_E2E") != "1",
    reason="requires live dbsync (FLUID_E2E=1)",
)


def test_every_live_pool_loan_and_request_converts():
    load_dotenv()
    from charli3_dendrite.backend.dbsync import DbsyncBackend
    from charli3_dendrite.lending.fluidtokens_v4.loader import snapshot
    from charli3_dendrite.lending.fluidtokens_v4.market import pool_to_market
    from charli3_dendrite.lending.fluidtokens_v4.market import loan_to_position
    from charli3_dendrite.lending.fluidtokens_v4.market import (
        request_to_borrow_request,
    )

    live = snapshot(DbsyncBackend())
    assert live.pools, "no live pools"
    managers = {manager.pool_id: manager for manager in live.pool_managers}
    for pool in live.pools:
        market = pool_to_market(pool, managers.get(pool.pool_id))
        assert market.available_liquidity >= 0
    for loan in live.loans:
        position = loan_to_position(loan)
        assert position.current_debt(live.now_ms) >= 0
    for request in live.requests:
        request_to_borrow_request(request)
