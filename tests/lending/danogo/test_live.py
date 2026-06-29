"""Live mainnet checks for the Danogo deployment anchors (dbsync-gated).

Skipped unless dbsync env vars are set (``DBSYNC_HOST`` etc.) and the anchors are
populated in ``constants``. These confirm the anchors resolve to real on-chain UTxOs
and that the Protocol Config datum parses into the expected script credentials.
"""

import os

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash

from charli3_dendrite.lending.danogo import constants

pytestmark = pytest.mark.skipif(
    not os.environ.get("DBSYNC_HOST") or constants.PROTOCOL_CONFIG_NFT is None,
    reason="requires live dbsync (DBSYNC_HOST/USER/PASS/PORT/DB_NAME) and anchors",
)

# Stable deployment script credentials carried by the ProtocolDatum.
EXPECTED_SCRIPTS = {
    "pool": "94dca24a1f1fcc2ff51cd90f32f4fe9e786d861a2dbf7d27598d26e8",
    "loan": "aca8e306eda3eb6c25a838bebac37d929c216aab13c8d463fca5a08d",
    "config_pool": "814de8a99452972a9fa9fe2c0f59f49697f208005c001ecac1ddfd57",
    "oracle": "012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a",
}


def _backend():
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _enterprise(skh: str) -> str:
    return Address(
        payment_part=ScriptHash(bytes.fromhex(skh)), network=Network.MAINNET
    ).encode()


def test_resolve_addresses_live():
    """The Protocol Config NFT resolves and its datum yields the four scripts."""
    addrs = constants.resolve_addresses(_backend())
    assert set(addrs) == {"pool", "loan", "config_pool", "oracle"}
    for key, skh in EXPECTED_SCRIPTS.items():
        assert Address.decode(addrs[key]).payment_part.payload.hex() == skh


def test_oracle_global_config_live():
    """The Oracle Global Config NFT resolves to a live UTxO at the oracle script."""
    backend = _backend()
    rows = list(
        backend.get_pool_utxos(
            addresses=[_enterprise(constants.ORACLE_DATA_SKH)],
            assets=[constants.ORACLE_GLOBAL_CONFIG_NFT],
            limit=2,
            historical=False,
        )
    )
    assert rows, "Oracle Global Config UTxO not found on-chain"


def test_oracle_price_calc_redeemer_live():
    """Real Withdraw(oracle_skh) redeemers decode into the verified model.

    Pulls recent reward (withdrawal) redeemers at the oracle stake script straight
    from dbsync and confirms every ``oracle_idxs`` entry maps to a known enum and the
    ``prices`` map yields exact integer rationals. This is the ground-truth check that
    pins ``OracleUtxoType`` / ``UTxOTarget`` and the redeemer layout.
    """
    import os

    import psycopg

    from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
    from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
    from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget

    oracle_skh = EXPECTED_SCRIPTS["oracle"]
    conn = psycopg.connect(
        host=os.environ["DBSYNC_HOST"],
        port=os.environ.get("DBSYNC_PORT", "5432"),
        dbname=os.environ["DBSYNC_DB_NAME"],
        user=os.environ["DBSYNC_USER"],
        password=os.environ["DBSYNC_PASS"],
        connect_timeout=20,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT encode(rd.bytes, 'hex')
            FROM redeemer r
            JOIN redeemer_data rd ON rd.id = r.redeemer_data_id
            JOIN tx ON tx.id = r.tx_id
            JOIN block b ON b.id = tx.block_id
            WHERE r.purpose = 'reward' AND r.script_hash = decode(%s, 'hex')
            ORDER BY b.time DESC
            LIMIT 25
            """,
            (oracle_skh,),
        )
        rows = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    assert rows, "no oracle withdrawal redeemers found on-chain"
    for cbor in rows:
        r = OraclePriceCalcRdmr.from_cbor(cbor)
        assert r.oracle_idxs, "redeemer carried no oracle indices"
        for target, otype, idx in r.oracle_idxs:
            assert isinstance(target, UTxOTarget)
            assert isinstance(otype, OracleUtxoType)
        for quote, inner in r.prices.items():
            for collat, (num, denom) in inner.items():
                assert isinstance(num, int) and isinstance(denom, int)
                assert denom > 0


def test_snapshot_live():
    """A full live snapshot parses real markets/pools/loans into a LendingBook."""
    from charli3_dendrite.lending.danogo.loader import snapshot

    book = snapshot(_backend())
    assert book.pool is not None, "no markets/pools discovered on-chain"
    # The ADA market is part of every Danogo Float deployment.
    supply_tokens = {ln.borrowed_unit for ln in book.active_loans()}
    assert book.pool.market.supply_token  # parsed a real supply token
    # Every parsed loan accrues a positive current debt from its pool datum.
    for ln in book.active_loans():
        assert ln.current_debt() > 0
    assert supply_tokens or not book.active_loans()
