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
    tx_hash: str
    tx_index: int
    block_height: int
    block_time: int
    address: str
    datum_hash: str
    inline_datum_id: Optional[int]
    datum: Dict[str, Any]
    datum_cbor: Optional[str]
    assets: Assets

    @classmethod
    def from_dbsync(cls, item: List[Any]):
        assets = Assets(
            lovelace=item[8], **{asset[0] + asset[1]: asset[2] for asset in item[9]}
        )

        if len(item) > 10:
            datum_cbor = item[10]
        else:
            datum_cbor = None

        return cls(
            tx_hash=item[0],
            tx_index=item[1],
            block_height=item[2],
            block_time=item[3],
            address=item[4],
            datum_hash=item[5],
            inline_datum_id=item[6],
            datum=item[7],
            datum_cbor=datum_cbor,
            assets=assets,
        )


def get_transactions_by_policy(
    policies: Optional[List[str]] = None,
    addresses: Optional[List[str]] = None,
    limit: int = 1000,
    page: int = 0,
    policy_only=False,
    historical=False,
):
    if policies is None and addresses is None:
        raise ValueError("Either policies or addresses must be defined.")

    if policies is not None and not isinstance(policies, list):
        raise ValueError("policies must be a list of strings.")

    if addresses is not None and not isinstance(addresses, list):
        raise ValueError("addresses must be a list of strings.")

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
            datum.bytes,
            ENCODE(txo.data_hash, 'hex') as "data_hash"
            FROM ma_tx_out mto
            JOIN multi_asset ma ON (mto.ident = ma.id)
            JOIN tx_out txo ON (mto.tx_out_id = txo.id)
            JOIN datum ON (txo.data_hash = datum.hash)"""
    )

    if not historical:
        DATUM_SELECTOR += """
            JOIN tx_in ON ((txo.tx_id = tx_in.tx_out_id) AND ((txo.index)::smallint = (tx_in.tx_out_index)::smallint))
            JOIN block ON (txo.tx_id = block.id)"""

    if policies is not None:
        if policy_only:
            DATUM_SELECTOR += """
                WHERE (encode(policy, 'hex')) = ANY(%(policy)s)"""
        else:
            DATUM_SELECTOR += """
                WHERE (encode(policy, 'hex') || encode(name, 'hex')) = ANY(%(policy)s)"""
    else:
        DATUM_SELECTOR += """
            WHERE address = ANY(%(address)s)"""

    if not historical:
        DATUM_SELECTOR += (
            """ AND (tx_in.tx_in_id is NULL) AND (block.epoch_no is not NULL)"""
        )

    DATUM_SELECTOR += """
            GROUP BY txo.tx_id, txo.id, datum.value, datum.bytes
            ORDER BY txo.tx_id ASC
            LIMIT %(limit)s
            OFFSET %(offset)s
        ) AS "sorted_limited"
        JOIN tx ON (sorted_limited.tx_id = tx.id)
        JOIN block b ON (b.id = tx.block_id)
        JOIN ma_tx_out mto ON (sorted_limited.tx_out_id = mto.tx_out_id)
        JOIN multi_asset ma ON (mto.ident = ma.id)
        GROUP BY tx.hash, tx.block_index, b.block_no, b.time, sorted_limited.value,
            sorted_limited.coin, sorted_limited.inline_datum_id, sorted_limited.data_hash,
            sorted_limited.address, sorted_limited.bytes
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
