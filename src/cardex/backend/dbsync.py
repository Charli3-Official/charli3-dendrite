# noqa
import os

import psycopg_pool
from dotenv import load_dotenv

from cardex.dataclasses.models import BlockList
from cardex.dataclasses.models import PoolStateList

load_dotenv()

DBSYNC_USER = os.environ.get("DBSYNC_USER", None)
DBSYNC_PASS = os.environ.get("DBSYNC_PASS", None)
DBSYNC_HOST = os.environ.get("DBSYNC_HOST", None)
DBSYNC_PORT = os.environ.get("DBSYNC_PORT", None)

if DBSYNC_HOST is not None:
    conninfo = (
        f"host={DBSYNC_HOST} port={DBSYNC_PORT} dbname=cexplorer "
        + f"user={DBSYNC_USER} password={DBSYNC_PASS}"
    )
    pool = psycopg_pool.ConnectionPool(
        conninfo=conninfo,
        open=False,
        min_size=1,
        max_size=10,
    )
    pool.open()
    pool.wait()


def db_query(query: str, args: tuple | None = None) -> list[tuple]:
    """Fetch results from a query."""
    with pool.connection() as conn:  # noqa: SIM117
        with conn.cursor() as cursor:
            cursor.execute(query, args)
            return cursor.fetchall()


POOL_SELECTOR = """
SELECT txo.address,
ENCODE(tx.hash, 'hex') as "tx_hash",
txo.index,
EXTRACT(
	epoch
	FROM block.time
)::INTEGER AS "block_time",
ENCODE(block.hash,'hex') as "block_hash",
ENCODE(datum.hash,'hex') as "datum_hash",
ENCODE(datum.bytes,'hex') as "datum_cbor",
txo.value::TEXT,
(
	SELECT json_agg(
		json_build_object(
				'unit',
				CONCAT(encode(ma.policy, 'hex'), encode(ma.name, 'hex')),
				'quantity',
				mto.quantity::TEXT
		)
	)
	FROM ma_tx_out mto
	JOIN multi_asset ma ON (mto.ident = ma.id)
	WHERE mto.tx_out_id = txo.id
) AS "amount"
"""


def get_pool_utxos(
    assets: list[str] | None = None,
    addresses: list[str] | None = None,
    limit: int = 1000,
    page: int = 0,
    historical: bool = True,
) -> PoolStateList:
    """Get transactions by policy or address."""
    error_msg = "Either policies or addresses must be defined, not both."
    if assets is None and addresses is None:
        raise ValueError(error_msg)

    if assets is not None and addresses is not None:
        raise ValueError(error_msg)

    # Use the pool selector to format the output
    datum_selector = POOL_SELECTOR

    # If assets are specified, select assets
    if assets is not None:
        datum_selector += """FROM (
    SELECT ma.policy, ma.name, ma.id
    FROM multi_asset ma
    WHERE policy = ANY(%(policies)b) AND name = ANY(%(names)b)
) as ma
JOIN ma_tx_out mtxo ON ma.id = mtxo.ident
LEFT JOIN tx_out txo ON mtxo.tx_out_id = txo.id
"""

    # If address is specified, select addresses
    else:
        datum_selector += """FROM (
    SELECT *
    FROM tx_out
    WHERE tx_out.address = ANY(%(addresses)s)
) as txo"""

    datum_selector += """
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id"""

    if not historical:
        datum_selector += """
LEFT JOIN tx_in ON tx_in.tx_out_id = txo.tx_id AND tx_in.tx_out_index = txo.index
WHERE tx_in.tx_in_id IS NULL AND datum.hash IS NOT NULL
"""
    else:
        datum_selector += """
WHERE datum.hash IS NOT NULL
"""

    datum_selector += """
LIMIT %(limit)s
OFFSET %(offset)s
"""

    values = {"limit": limit, "offset": page * limit}
    if assets is not None:
        values.update({"policies": [bytes.fromhex(p[:56]) for p in assets]})
        values.update({"names": [bytes.fromhex(p[56:]) for p in assets]})

    elif addresses is not None:
        values.update({"addresses": addresses})

    r = db_query(datum_selector, values)

    return PoolStateList.from_dbsync(r)


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
    return BlockList.from_dbsync(r)


def get_pool_utxos_in_block(block_no: int) -> PoolStateList:
    """Get pool utxos in block."""
    # Use this for gathering all assets for multiple addresses
    datum_selector = (
        POOL_SELECTOR
        + """
FROM tx_out txo
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id
WHERE block.block_no = %(block_no)s AND datum.hash IS NOT NULL
"""
    )
    r = db_query(datum_selector, {"block_no": block_no})

    return PoolStateList.from_dbsync(r)
