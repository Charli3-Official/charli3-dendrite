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
        conninfo=conninfo, open=False, min_size=4, max_size=50
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
        SELECT encode(tx.hash, 'hex') AS "tx_hash",
        tx.block_index AS "tx_index",
        b.block_no AS "block_height",
        extract(
            epoch
            FROM b.time
        )::INTEGER AS "block_time",
        sorted_limited.address,
        sorted_limited.data_hash,
        sorted_limited.inline_datum_id,
        sorted_limited.value,
        sorted_limited.coin,
        json_agg(json_build_array(ENCODE(ma.policy::bytea, 'hex'), encode((ma.name)::bytea, 'hex'::text), mto.quantity)) AS assets,
        sorted_limited.bytes"""


class DBSyncPoolState(BaseModel):
    address: str
    tx_hash: str
    tx_index: int
    datum_hash: str
    inline_datum_id: Optional[int]
    datum: Dict[str, Any]
    datum_cbor: Optional[str]
    assets: Assets

    @classmethod
    def from_dbsync(cls, item: List[Any]):
        assets = Assets(
            lovelace=item[7], **{asset[0] + asset[1]: asset[2] for asset in item[8]}
        )

        return cls(
            address=item[0],
            tx_hash=item[1],
            tx_index=item[2],
            datum_hash=item[3],
            inline_datum_id=item[4],
            datum=item[5],
            datum_cbor=item[6],
            assets=assets,
        )


def get_transactions(
    policies: Optional[List[str]] = None,
    names: Optional[List[str]] = None,
    addresses: Optional[List[str]] = None,
    limit: int = 1000,
    page: int = 0,
    policy_only=False,
):
    """Get transactions by policy or address."""
    if policies is None and addresses is None:
        raise ValueError("Either policies or addresses must be defined.")

    if policies is not None and not isinstance(policies, list):
        raise ValueError("policies must be a list of strings.")

    if addresses is not None and not isinstance(addresses, list):
        raise ValueError("addresses must be a list of strings.")

    # Use this for gathering all assets for multiple addresses
    DATUM_SELECTOR = """SELECT sl.address,
    encode(tx.hash, 'hex') AS "tx_hash",
    sl.index AS "tx_index",
    sl.data_hash,
    sl.inline_datum_id,
    datum.value,
    datum.bytes,
    sl.value,
    json_agg(json_build_array(ENCODE(sl.policy::bytea, 'hex'), encode((sl.name)::bytea, 'hex'::text), sl.quantity)) AS assets
    FROM (
        SELECT * FROM (
            SELECT ma.policy, ma.name, ma.id 
            FROM multi_asset ma"""

    if policies is not None:
        DATUM_SELECTOR += """
            WHERE policy = ANY(%(policies)b)"""

        if names is not None:
            DATUM_SELECTOR += """ AND name = ANY(%(names)b)"""

    DATUM_SELECTOR += """
        ) as ma
        JOIN ma_tx_out mtxo ON ma.id = mtxo.ident
        JOIN tx_out txo ON mtxo.tx_out_id = txo.id"""

    if addresses is not None:
        DATUM_SELECTOR += """
        WHERE tx_out.address = ANY(%(addresses)s)"""

    DATUM_SELECTOR += """
        LIMIT %(limit)s
        OFFSET %(offset)s
    ) as sl
    JOIN datum ON sl.data_hash = datum.hash
    JOIN tx ON sl.tx_id = tx.id
    GROUP BY sl.address, tx.hash, sl.index, sl.data_hash, sl.inline_datum_id,
        datum.value, datum.bytes, sl.value, sl.tx_id
    ORDER BY sl.tx_id ASC
    """
    values = {"limit": limit, "offset": page * limit}
    if policies is not None:
        values.update({"policy": policies})

    if addresses is not None:
        values.update({"address": addresses})

    r = select_fetchall(DATUM_SELECTOR, values)

    return r


def last_block():
    r = select_fetchall(
        """select epoch_slot_no, block_no, tx_count from block where block_no is not null
           order by block_no desc limit 10 ;"""
    )
    return r


def get_transactions_in_block(block_no: int):
    # Use this for gathering all assets for multiple addresses
    DATUM_SELECTOR = (
        POOL_SELECTOR
        + """
        FROM (
            SELECT txo.tx_id AS "tx_id",
            txo.address AS "address",
            txo.id AS "tx_out_id",
            txo.value AS "coin",
            txo.inline_datum_id,
            datum.value,
            ENCODE(txo.data_hash, 'hex') as "data_hash"
            FROM ma_tx_out mto
            JOIN multi_asset ma ON (mto.ident = ma.id)
            JOIN tx_out txo ON (mto.tx_out_id = txo.id)
            JOIN datum ON (txo.data_hash = datum.hash)
            JOIN tx ON (tx.id = txo.tx_id)
            JOIN block ON (tx.block_id = block.id)
            WHERE (block.block_no = %(block_no)s)
            GROUP BY txo.tx_id, txo.id, datum.value
            ORDER BY txo.tx_id DESC
        ) AS "sorted_limited"
        JOIN tx ON (sorted_limited.tx_id = tx.id)
        JOIN block b ON (b.id = tx.block_id)
        JOIN ma_tx_out mto ON (sorted_limited.tx_out_id = mto.tx_out_id)
        JOIN multi_asset ma ON (mto.ident = ma.id)
        GROUP BY tx.hash, tx.block_index, b.block_no, b.time, sorted_limited.value,
            sorted_limited.coin, sorted_limited.inline_datum_id, sorted_limited.data_hash,
            sorted_limited.address
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


def test_query():
    r = select_fetchall(
        """SELECT * FROM (
        SELECT ma.policy, ma.name, ma.id 
        FROM multi_asset ma 
        WHERE policy = DECODE('13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f', 'hex') AND name = DECODE('4d494e53574150', 'hex') 
    ) as ma
    JOIN ma_tx_out mtxo ON ma.id = mtxo.ident 
    JOIN tx_out txo ON mtxo.tx_out_id = txo.id 
    LIMIT 100
    OFFSET 0"""
    )

    return r
