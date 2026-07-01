"""TopupWithdrawSnapshot.from_backend resolves the live deposit/withdraw blocks.

Gated on a reachable db-sync (set DBSYNC_HOST/PORT/USER/PASS/DB_NAME) and a
DANOGO_TEST_MARKET pool-NFT name, matching the create-loan snapshot test idiom.
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()
from charli3_dendrite.backend.dbsync import DbsyncBackend  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    TopupWithdrawSnapshot,
)

MARKET = os.environ.get("DANOGO_TEST_MARKET", "")


@pytest.mark.skipif(
    not (os.environ.get("DBSYNC_HOST") and MARKET),
    reason="needs db-sync + DANOGO_TEST_MARKET",
)
def test_topup_withdraw_snapshot_resolves():
    snap = TopupWithdrawSnapshot.from_backend(DbsyncBackend(), market_name=MARKET)
    assert snap.pool_script_ref is not None
    assert snap.pool.out_ref is not None
    assert snap.market_info.supply_token
