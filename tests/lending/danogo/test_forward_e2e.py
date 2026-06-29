"""Gated (db-sync) end-to-end validation of Danogo forward pricing.

Both tests need a reachable mainnet db-sync; they skip otherwise so the suite
stays green without a backend.
"""
import os

import pytest


@pytest.mark.skipif(not os.environ.get("DBSYNC_HOST"), reason="needs db-sync")
def test_live_snapshot_prices_markets():
    import psycopg
    from charli3_dendrite.backend.dbsync import DbsyncBackend
    from charli3_dendrite.lending.danogo.loader import snapshot
    from charli3_dendrite.lending.oracles.models import OracleSource

    backend = DbsyncBackend()
    try:
        book = snapshot(backend)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"db-sync unreachable: {exc}")

    priced = book.prices.prices
    print(f"\nlive snapshot: {len(priced)} collateral tokens priced")
    for token, p in sorted(priced.items()):
        print(f"  {token[:24]}... = {p.num}/{p.denom} in {p.quote[:16]} ({p.source})")
    # Every emitted price must be well-formed and from the Danogo aggregator.
    for p in priced.values():
        assert p.num > 0 and p.denom > 0
        assert p.source == OracleSource.DANOGO_AGGREGATOR
    # Live pricing must actually work (the feature's whole point). 8 is a conservative
    # floor for ~28 markets; if the real number is far below, that's a defect to report.
    assert len(priced) >= 8


@pytest.mark.skipif(not os.environ.get("DBSYNC_HOST"), reason="needs db-sync")
def test_reproduction_is_robust_over_sample():
    import sys
    from pathlib import Path

    import psycopg

    fixtures = Path(__file__).parent / "fixtures"
    sys.path.insert(0, str(fixtures))
    import _capture_forward_pricing as cap
    from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr

    try:
        conn = psycopg.connect(**cap.CONN)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"db-sync unreachable: {exc}")
    cur = conn.cursor()
    cur.execute(
        """SELECT encode(tx.hash,'hex') FROM redeemer r JOIN tx ON tx.id=r.tx_id
           WHERE r.purpose='reward' AND r.script_hash=decode(%s,'hex')
           ORDER BY r.id DESC LIMIT 300""",
        (cap.ORACLE_SKH,),
    )
    txs = [h for (h,) in cur.fetchall()]
    reproduced = 0
    seen_pairs = set()
    for txh in txs:
        cbor = cap._reward_redeemer_cbor(cur, txh)
        if not cbor:
            continue
        try:
            rdmr = OraclePriceCalcRdmr.from_cbor(cbor)
        except Exception:
            continue
        refs = cap._ref_inputs(cur, txh)
        for quote, inner in rdmr.prices.items():
            for collat, (num, denom) in inner.items():
                if (num, denom) == (1, 1):
                    continue
                key = (collat, quote)
                if key in seen_pairs:
                    continue
                case = cap._case(rdmr, refs, quote, collat, num, denom)
                if case is not None:
                    seen_pairs.add(key)
                    reproduced += 1
                    # each reproduced price is a single ordered product (no averaging)
                    assert case["leaves"]
    conn.close()
    print(f"\nreproduced {reproduced} distinct (collateral,quote) pairs from 300 txs")
    assert reproduced >= 25
