"""CreateLoanSnapshot.from_backend resolves the live building blocks for a market.

Gated on a reachable db-sync (set DBSYNC_HOST/PORT/USER/PASS/DB_NAME). The snapshot's
derived script hashes are cross-checked against the captured create-loan fixture so a
regression in resolution is caught even though the live UTxOs themselves rotate.
"""

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

from charli3_dendrite.lending.danogo.constants import PROTOCOL_CONFIG_NFT  # noqa: E402
from charli3_dendrite.lending.danogo.constants import resolve_addresses  # noqa: E402
from charli3_dendrite.lending.danogo.loader import fetch_markets  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    ORACLE_SKH,
    CreateLoanSnapshot,
)

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)

MARKET = os.environ.get("DANOGO_TEST_MARKET", "")


@pytest.mark.skipif(
    not os.environ.get("DBSYNC_HOST"),
    reason="needs a reachable db-sync (DBSYNC_HOST/PORT/USER/PASS/DB_NAME)",
)
def test_snapshot_from_backend_resolves_a_live_market():
    import psycopg

    from charli3_dendrite.backend.dbsync import DbsyncBackend

    backend = DbsyncBackend()
    try:
        addresses = resolve_addresses(backend)
        markets = fetch_markets(backend, addresses)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"db-sync unreachable: {exc}")

    assert markets, "no Danogo markets resolved"
    market_name = next(iter(markets))

    snap = CreateLoanSnapshot.from_backend(backend, market_name=market_name)

    # Derived script hashes are deployment-stable: cross-check against the fixture.
    assert snap.loan_skh == FIX["loan_skh"]
    assert snap.oracle_skh == ORACLE_SKH

    # The Protocol Config reference UTxO actually holds the config NFT.
    cfg_policy, cfg_name = PROTOCOL_CONFIG_NFT[:56], PROTOCOL_CONFIG_NFT[56:]
    assert snap.protocol_config.holds(cfg_policy, cfg_name)
    assert snap.protocol_config.out_ref is not None

    # Market + pool are spendable/referenceable and share the pool-NFT name.
    assert snap.market.out_ref is not None
    assert snap.pool.out_ref is not None
    assert snap.market.holds(snap.config_pool_skh, market_name)
    assert snap.pool.holds(snap.config_pool_skh, market_name)
    assert snap.market_info.supply_token

    # Danogo-owned oracle reference UTxOs are present (leaves are resolved later).
    assert snap.oracle_data_refs


@pytest.mark.skipif(
    not (os.environ.get("DBSYNC_HOST") and os.environ.get("DANOGO_TEST_MARKET")),
    reason="needs db-sync + DANOGO_TEST_MARKET",
)
def test_snapshot_resolves_script_refs_and_oracle_leaves():
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    snap = CreateLoanSnapshot.from_backend(DbsyncBackend(), market_name=MARKET)

    # The three script-reference UTxOs the forward build attaches as reference scripts.
    for ref in (
        snap.pool_script_ref,
        snap.loan_mint_script_ref,
        snap.oracle_script_ref,
    ):
        assert ref is not None
        assert ref.ref_script is not None
        assert ref.out_ref is not None

    # The oracle source leaves the forward build adds as reference inputs.
    assert len(snap.oracle_source_leaves) >= 1
    for leaf in snap.oracle_source_leaves:
        assert leaf.out_ref is not None
        assert leaf.datum is not None
