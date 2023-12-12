import decimal
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import repeat
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg_pool
from cardex.utility import Assets
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

DBSYNC_USER = os.environ.get("DBSYNC_USER", None)
DBSYNC_PASS = os.environ.get("DBSYNC_PASS", None)
DBSYNC_HOST = os.environ.get("DBSYNC_HOST", None)
DBSYNC_PORT = os.environ.get("DBSYNC_PORT", None)

if DBSYNC_HOST is not None:
    conninfo = f"host={DBSYNC_HOST} port={DBSYNC_PORT} dbname=cexplorer user={DBSYNC_USER} password={DBSYNC_PASS}"
    pool = psycopg_pool.ConnectionPool(
        conninfo=conninfo, open=False, min_size=1, max_size=10
    )
    pool.open()
    pool.wait()


def select_fetchone(query, args=None):
    with pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, args)
            results = cursor.fetchone()
            return results


def select_fetchall(query, args=None):
    with pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, args)
            results = cursor.fetchall()
            return results


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


class DBSyncPoolState(BaseModel):
    address: str
    tx_hash: str
    tx_index: int
    block_time: int
    block_hash: str
    datum_hash: str
    datum_cbor: Optional[str]
    assets: Assets

    @classmethod
    def from_dbsync(cls, item: List[Any]):
        assets = Assets(lovelace=item[7], **{a["unit"]: a["quantity"] for a in item[8]})

        return cls(
            address=item[0],
            tx_hash=item[1],
            tx_index=item[2],
            block_time=item[3],
            block_hash=item[4],
            datum_hash=item[5],
            datum_cbor=item[6],
            assets=assets,
        )


class LastBlock(BaseModel):
    epoch_slot_no: int
    block_no: int
    tx_index: int
    block_time: int

    @classmethod
    def from_dbsync(cls, item: List[Any]):
        return cls(
            epoch_slot_no=item[0],
            block_no=item[1],
            tx_index=item[2],
            block_time=item[3],
        )


def get_historical_utxos(
    assets: Optional[List[str]] = None,
    addresses: Optional[List[str]] = None,
    limit: int = 1000,
    page: int = 0,
    historical: bool = True,
):
    """Get transactions by policy or address."""
    if assets is None and addresses is None:
        raise ValueError("Either policies or addresses must be defined.")
    elif assets is not None and addresses is not None:
        raise ValueError("Either policies or addresses must be defined, not both.")

    # Use the pool selector to format the output
    DATUM_SELECTOR = POOL_SELECTOR

    # If assets are specified, select assets
    if assets is not None:
        DATUM_SELECTOR += """FROM (
    SELECT ma.policy, ma.name, ma.id 
    FROM multi_asset ma
    WHERE policy = ANY(%(policies)b) AND name = ANY(%(names)b)
) as ma
JOIN ma_tx_out mtxo ON ma.id = mtxo.ident
LEFT JOIN tx_out txo ON mtxo.tx_out_id = txo.id
"""

    # If address is specified, select addresses
    else:
        DATUM_SELECTOR += """FROM (
    SELECT *
    FROM tx_out
    WHERE tx_out.address = ANY(%(addresses)s)
) as txo"""

    DATUM_SELECTOR += """
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id"""

    if not historical:
        DATUM_SELECTOR += """
LEFT JOIN tx_in ON tx_in.tx_out_id = txo.tx_id AND tx_in.tx_out_index = txo.index
WHERE tx_in.tx_in_id IS NULL
"""

    DATUM_SELECTOR += """
LIMIT %(limit)s
OFFSET %(offset)s
"""

    values = {"limit": limit, "offset": page * limit}
    if assets is not None:
        values.update({"policies": [bytes.fromhex(p[:56]) for p in assets]})
        values.update({"names": [bytes.fromhex(p[56:]) for p in assets]})

    elif addresses is not None:
        values.update({"addresses": addresses})

    r = select_fetchall(DATUM_SELECTOR, values)

    return r


def last_block():
    r = select_fetchall(
        """SELECT epoch_slot_no,
block_no,
tx_count,
EXTRACT(
	epoch
	FROM block.time
)::INTEGER AS "block_time"
FROM block
WHERE block_no IS NOT null
ORDER BY block_no DESC
LIMIT 2"""
    )
    return r


def get_datum_transactions_in_block(block_no: int):
    # Use this for gathering all assets for multiple addresses
    DATUM_SELECTOR = (
        POOL_SELECTOR
        + """
FROM tx_out txo
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id
WHERE block.block_no = %(block_no)s AND datum.hash IS NOT NULL
"""
    )
    r = select_fetchall(
        DATUM_SELECTOR,
        {"block_no": block_no},
    )
    return r


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, decimal.Decimal):
            return int(obj)
        elif isinstance(obj, bytes):
            return obj.hex()

        return json.JSONEncoder.default(self, obj)
