# noqa
import os
from threading import Lock

import psycopg_pool
from dotenv import load_dotenv
from psycopg.rows import dict_row

from cardex.dataclasses.models import BlockList

load_dotenv()

lock = Lock()

POOL = None

DBSYNC_USER = os.environ.get("DBSYNC_USER", None)
DBSYNC_PASS = os.environ.get("DBSYNC_PASS", None)
DBSYNC_HOST = os.environ.get("DBSYNC_HOST", None)
DBSYNC_PORT = os.environ.get("DBSYNC_PORT", None)
DBSYNC_DB_NAME = os.environ.get("DBSYNC_DB_NAME", None)


def get_dbsync_pool() -> psycopg_pool.ConnectionPool:
    """Get a postgres connection."""
    global POOL  # noqa
    with lock:
        if POOL is None:
            conninfo = (
                f"host={DBSYNC_HOST} port={DBSYNC_PORT} dbname={DBSYNC_DB_NAME} "
                + f"user={DBSYNC_USER} password={DBSYNC_PASS}"
            )
            POOL = psycopg_pool.ConnectionPool(
                conninfo=conninfo,
                open=False,
                min_size=1,
                max_size=30,
                max_idle=10,
                reconnect_timeout=30,
                max_lifetime=60,
                check=psycopg_pool.ConnectionPool.check_connection,
            )
            POOL.open()
            POOL.wait()
    return POOL


def db_query(query: str, args: tuple | None = None) -> list[tuple]:
    """Fetch results from a query."""
    with get_dbsync_pool().connection() as conn:  # noqa: SIM117
        with conn.cursor(row_factory=dict_row) as cursor:
            # with conn.cursor() as cursor:
            cursor.execute(query, args)
            return cursor.fetchall()


def last_block(last_n_blocks: int = 2) -> BlockList:
    """Get the last n blocks."""
    r = db_query(
        """
SELECT epoch_slot_no,
block_no,
tx_count,
EXTRACT(
	epoch
	FROM block.time
)::INTEGER AS "block_time"
FROM block
WHERE block_no IS NOT null
ORDER BY block_no DESC
LIMIT %(last_n_blocks)s""",
        {"last_n_blocks": last_n_blocks},
    )
    return BlockList.model_validate(r)
