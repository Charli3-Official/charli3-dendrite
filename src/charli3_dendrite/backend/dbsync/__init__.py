"""Concrete implementation of AbstractBackend for db-sync."""
import logging
import os
from datetime import datetime
from threading import Lock

import psycopg_pool  # type: ignore
from dotenv import load_dotenv  # type: ignore
from psycopg.rows import dict_row  # type: ignore
from psycopg_pool import PoolTimeout
from pycardano import Address  # type: ignore

from charli3_dendrite.backend.backend_base import AbstractBackend
from charli3_dendrite.backend.dbsync.models import OrderSelector
from charli3_dendrite.backend.dbsync.models import PoolSelector
from charli3_dendrite.backend.dbsync.models import UTxOSelector
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import BlockList
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dataclasses.models import SwapTransactionList

load_dotenv()


class DbsyncBackend(AbstractBackend):
    """Concrete implementation of AbstractBackend for db-sync.

    This class provides methods to interact with a Cardano db-sync database
    for retrieving blockchain data.
    """

    def __init__(self) -> None:
        """Initialize the DbsyncBackend with database connection details."""
        self.lock = Lock()
        self.POOL = None
        self.DBSYNC_USER = os.environ.get("DBSYNC_USER", None)
        self.DBSYNC_PASS = os.environ.get("DBSYNC_PASS", None)
        self.DBSYNC_HOST = os.environ.get("DBSYNC_HOST", None)
        self.DBSYNC_PORT = os.environ.get("DBSYNC_PORT", None)
        self.DBSYNC_DB_NAME = os.environ.get("DBSYNC_DB_NAME", None)

    def get_dbsync_pool(self) -> psycopg_pool.ConnectionPool:
        """Get or create a connection pool for the db-sync database.

        Returns:
            psycopg_pool.ConnectionPool: A connection pool for database operations.
        """
        with self.lock:
            if self.POOL is None:
                conninfo = (
                    f"host={self.DBSYNC_HOST} "
                    + f"port={self.DBSYNC_PORT} "
                    + f"dbname={self.DBSYNC_DB_NAME} "
                    + f"user={self.DBSYNC_USER} "
                    + f"password={self.DBSYNC_PASS}"
                )
                self.POOL = psycopg_pool.ConnectionPool(
                    conninfo=conninfo,
                    open=False,
                    min_size=1,
                    max_size=10,
                    max_idle=10,
                    reconnect_timeout=30,  # Increased from 10 to 30
                    max_lifetime=60,
                    check=psycopg_pool.ConnectionPool.check_connection,
                )
                try:
                    if self.POOL is None:
                        raise ValueError("Connection pool has not been initialized.")

                    self.POOL.open()
                    self.POOL.wait(timeout=60.0)  # Increased from 30 to 60 seconds
                except PoolTimeout as e:
                    logging.error(
                        f"Database connection pool initialization timed out: {e}",
                    )
                    logging.error(
                        f"Connection info: host={self.DBSYNC_HOST}, "
                        + f"port={self.DBSYNC_PORT}, "
                        + f"user={self.DBSYNC_USER}",
                    )
                    raise
                except Exception as e:
                    logging.error(f"Error initializing database connection pool: {e}")
                    raise
        return self.POOL

    def db_query(self, query: str, args: dict | None = None) -> list[dict]:
        """Execute a database query using the connection pool.

        Args:
            query (str): The SQL query to execute.
            args (Optional[tuple]): Arguments to be used with the query.

        Returns:
            List[tuple]: The query results.
        """
        with self.get_dbsync_pool().connection() as conn, conn.cursor(
            row_factory=dict_row,
        ) as cursor:
            cursor.execute(query, args)
            return cursor.fetchall()

    def get_pool_utxos(
        self,
        addresses: list[str],
        assets: list[str] | None = None,
        limit: int = 1000,
        page: int = 0,
        historical: bool = True,
    ) -> PoolStateList:
        """Get transactions by policy or address.

        Args:
            addresses: A list of addresses for pool or order contracts
            assets: A list of assets used to filter utxos. Defaults to None.
            limit: Number of values to return. Defaults to 1000.
            page: Page of pools to return. Defaults to 0.
            historical: If False, returns current pool states. Defaults to True.

        Returns:
            A list of pool states.
        """
        # Use the pool selector to format the output
        datum_selector = PoolSelector.select()

        # Get txo from pool script address
        datum_selector += """FROM (
    SELECT *
    FROM tx_out
    WHERE tx_out.payment_cred = ANY(%(addresses)b)
) as txo"""

        # If assets are specified, select assets
        if assets is not None:
            datum_selector += """
LEFT JOIN ma_tx_out mtxo ON mtxo.tx_out_id = txo.id
LEFT JOIN multi_asset ma ON ma.id = mtxo.ident"""

        datum_selector += """
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id
WHERE datum.hash IS NOT NULL"""

        if not historical:
            datum_selector += """
AND txo.consumed_by_tx_id IS NULL"""

        if assets is not None:
            datum_selector += """
AND ma.policy = ANY(%(policies)b) AND ma.name = ANY(%(names)b)"""

        datum_selector += """
LIMIT %(limit)s
OFFSET %(offset)s"""

        values = {
            "limit": limit,
            "offset": page * limit,
            "addresses": [Address.decode(a).payment_part.payload for a in addresses],
        }
        if assets is not None:
            values.update({"policies": [bytes.fromhex(p[:56]) for p in assets]})
            values.update({"names": [bytes.fromhex(p[56:]) for p in assets]})

        r = self.db_query(datum_selector, values)

        return PoolSelector.parse(r)

    def get_pool_in_tx(
        self,
        tx_hash: str,
        addresses: list[str],
        assets: list[str] | None = None,
    ) -> PoolStateList:
        """Get transactions by policy or address."""
        # Use the pool selector to format the output
        datum_selector = PoolSelector.select()

        datum_selector += """FROM (
    SELECT *
    FROM tx_out
    WHERE tx_out.payment_cred = ANY(%(addresses)b)
) as txo"""

        # If assets are specified, select assets
        if assets is not None:
            datum_selector += """
LEFT JOIN ma_tx_out mtxo ON mtxo.tx_out_id = txo.id
LEFT JOIN multi_asset ma ON ma.id = mtxo.ident"""

        datum_selector += """
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id
WHERE datum.hash IS NOT NULL AND tx.hash = DECODE(%(tx_hash)s, 'hex')"""

        if assets is not None:
            datum_selector += """
AND ma.policy = ANY(%(policies)b) AND ma.name = ANY(%(names)b)"""

        values = {
            "tx_hash": tx_hash,
            "addresses": [Address.decode(a).payment_part.payload for a in addresses],
        }
        if assets is not None:
            values.update({"policies": [bytes.fromhex(p[:56]) for p in assets]})
            values.update({"names": [bytes.fromhex(p[56:]) for p in assets]})

        r = self.db_query(datum_selector, values)

        return PoolSelector.parse(r)

    def last_block(self, last_n_blocks: int = 2) -> BlockList:
        """Get the last n blocks."""
        r = self.db_query(
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

    def get_pool_utxos_in_block(self, block_no: int) -> PoolStateList:
        """Get pool utxos in block."""
        # Use this for gathering all assets for multiple addresses
        datum_selector = (
            PoolSelector.select()
            + """
    FROM tx_out txo
    LEFT JOIN tx ON txo.tx_id = tx.id
    LEFT JOIN datum ON txo.data_hash = datum.hash
    LEFT JOIN block ON tx.block_id = block.id
    WHERE block.block_no = %(block_no)s AND datum.hash IS NOT NULL
    """
        )
        r = self.db_query(datum_selector, {"block_no": block_no})

        return PoolSelector.parse(r)

    def get_script_from_address(self, address: Address) -> ScriptReference:
        """Get a reference script from an address."""
        query = UTxOSelector.select()

        query += """
FROM script s
LEFT JOIN tx_out ON s.id = tx_out.reference_script_id
LEFT JOIN tx ON tx.id = tx_out.tx_id
LEFT JOIN datum ON tx_out.inline_datum_id = datum.id
LEFT JOIN block on block.id = tx.block_id
WHERE s.hash = %(address)b AND tx_out.consumed_by_tx_id IS NULL
ORDER BY block.time DESC
LIMIT 1
"""
        r = self.db_query(query, {"address": address.payment_part.payload})

        if r[0]["assets"] is not None and r[0]["assets"][0]["lovelace"] is None:
            r[0]["assets"] = None

        return UTxOSelector.parse(r[0])

    def get_datum_from_address(
        self,
        address: Address,
        asset: str | None = None,
    ) -> ScriptReference | None:
        """Get a reference datum from an address."""
        kwargs = {"address": address.payment_part.payload}

        if asset is not None:
            kwargs.update(
                {
                    "policy": bytes.fromhex(asset[:56]),
                    "name": bytes.fromhex(asset[56:]),
                },
            )

        query = UTxOSelector.select()

        query += """
FROM tx_out
LEFT JOIN ma_tx_out mtxo ON mtxo.tx_out_id = tx_out.id
LEFT JOIN multi_asset ma ON ma.id = mtxo.ident
LEFT JOIN tx ON tx.id = tx_out.tx_id
LEFT JOIN datum ON tx_out.inline_datum_id = datum.id
LEFT JOIN block on block.id = tx.block_id
LEFT JOIN script s ON s.id = tx_out.reference_script_id
WHERE tx_out.payment_cred = %(address)b AND tx_out.consumed_by_tx_id IS NULL"""

        if asset is not None:
            query += """
AND policy = %(policy)b AND name = %(name)b
"""

        query += """
AND tx_out.inline_datum_id IS NOT NULL
ORDER BY block.time DESC
LIMIT 1
"""
        r = self.db_query(query, kwargs)

        if r[0]["assets"] is not None and r[0]["assets"][0]["lovelace"] is None:
            r[0]["assets"] = None

        return UTxOSelector.parse(r[0])

    def get_historical_order_utxos(
        self,
        stake_addresses: list[str],
        after_time: datetime | int | None = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get historical orders at an order submission address."""
        if isinstance(after_time, int):
            after_time = datetime.fromtimestamp(after_time)

        utxo_selector = OrderSelector.select()

        utxo_selector += """FROM (
    SELECT *
    FROM tx_out txo
    WHERE txo.payment_cred = ANY(%(addresses)b) AND txo.data_hash IS NOT NULL
) txo_stake
LEFT JOIN tx ON tx.id = txo_stake.tx_id
LEFT JOIN block ON tx.block_id = block.id
LEFT JOIN datum ON txo_stake.data_hash = datum.hash
LEFT JOIN (
    SELECT tx.hash AS "tx_hash",
    txo.index AS "tx_index",
    txo.value,
    txo.id as "tx_id",
    block.hash AS "block_hash",
    block.time AS "block_time",
    block.block_no,
    tx.block_index AS "block_index",
    tx_in.tx_id as "tx_out_id",
    tx_in.index as "tx_out_index",
    txo.inline_datum_id,
    txo.reference_script_id,
    txo.address,
    datum.hash as "datum_hash",
    datum.bytes as "datum_bytes"
    FROM tx_out tx_in
    LEFT JOIN tx ON tx.id = tx_in.consumed_by_tx_id
    LEFT JOIN tx_out txo ON tx.id = txo.tx_id
    LEFT JOIN block ON tx.block_id = block.id
    LEFT JOIN datum ON txo.data_hash = datum.hash
) txo_output ON txo_output.tx_out_id = txo_stake.tx_id
    AND txo_output.tx_out_index = txo_stake.index
WHERE datum.hash IS NOT NULL"""

        if after_time is not None:
            utxo_selector += """
AND block.time >= %(after_time)s"""

        utxo_selector += """
ORDER BY tx.id ASC
LIMIT %(limit)s
OFFSET %(offset)s"""

        r = self.db_query(
            utxo_selector,
            {
                "addresses": [
                    Address.decode(a).payment_part.payload for a in stake_addresses
                ],
                "limit": limit,
                "offset": page * limit,
                "after_time": (
                    None
                    if after_time is None
                    else after_time.strftime("%Y-%m-%d %H:%M:%S")
                ),
            },
        )

        return OrderSelector.parse(r)

    def get_order_utxos_by_block_or_tx(
        self,
        stake_addresses: list[str],
        out_tx_hash: list[str] | None = None,
        in_tx_hash: list[str] | None = None,
        block_no: int | None = None,
        after_block: int | None = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get order UTxOs by either block number or tx hash."""
        utxo_selector = """
    SELECT (
        SELECT array_agg(DISTINCT txo.address)
        FROM tx_out txo
        WHERE txo.consumed_by_tx_id = txo_stake.tx_id
    ) AS "submit_address_inputs",
    txo_stake.address as "submit_address_stake",
    ENCODE(txo_stake.tx_hash, 'hex') as "submit_tx_hash",
    txo_stake.index as "submit_tx_index",
    ENCODE(txo_stake.block_hash,'hex') as "submit_block_hash",
    EXTRACT(
        epoch
        FROM txo_stake.block_time
    )::INTEGER AS "submit_block_time",
    txo_stake.block_index AS "submit_block_index",
    (
        SELECT array_agg(tx_metadata.json)
        FROM tx_metadata
        WHERE txo_stake.tx_id = tx_metadata.tx_id
    ) AS "submit_metadata",
    COALESCE(
        json_build_object('lovelace',txo_stake.value::TEXT)::jsonb || (
            SELECT json_agg(
                json_build_object(
                    CONCAT(encode(ma.policy, 'hex'), encode(ma.name, 'hex')),
                    mto.quantity::TEXT
                )
            )
            FROM ma_tx_out mto
            JOIN multi_asset ma ON (mto.ident = ma.id)
            WHERE mto.tx_out_id = txo_stake.id
        )::jsonb,
        jsonb_build_array(json_build_object('lovelace',txo_stake.value::TEXT)::jsonb)
    ) AS "submit_assets",
    ENCODE(txo_stake.datum_hash,'hex') as "submit_datum_hash",
    ENCODE(txo_stake.datum_bytes,'hex') as "submit_datum_cbor",
    txo_output.address,
    ENCODE(txo_output.tx_hash, 'hex') as "tx_hash",
    txo_output.tx_index as "tx_index",
    EXTRACT(
        epoch
        FROM txo_output.block_time
    )::INTEGER AS "block_time",
    txo_output.block_index AS "block_index",
    ENCODE(txo_output.block_hash,'hex') AS "block_hash",
    ENCODE(txo_output.datum_hash, 'hex') AS "datum_hash",
    ENCODE(txo_output.datum_bytes, 'hex') AS "datum_cbor",
    COALESCE(
        json_build_object('lovelace',txo_output.value::TEXT)::jsonb || (
            SELECT json_agg(
                json_build_object(
                    CONCAT(encode(ma.policy, 'hex'), encode(ma.name, 'hex')),
                    mto.quantity::TEXT
                )
            )
            FROM ma_tx_out mto
            JOIN multi_asset ma ON (mto.ident = ma.id)
            WHERE mto.tx_out_id = txo_output.tx_id
        )::jsonb,
        jsonb_build_array(json_build_object('lovelace',txo_output.value::TEXT)::jsonb)
    ) AS "assets",
    (
        txo_output.inline_datum_id IS NOT NULL OR
        txo_output.reference_script_id IS NOT NULL
    ) as "plutus_v2"
    """

        utxo_selector += """FROM (
        SELECT DISTINCT txo.tx_id,
        txo.id,
        txo.index,
        txo.value,
        txo.data_hash,
        txo.address,
        tx.hash as "tx_hash",
        tx.block_index,
        block.hash as "block_hash",
        block.time as "block_time",
        datum.hash as "datum_hash",
        datum.bytes as "datum_bytes"
        FROM tx_out txo
        LEFT JOIN tx ON tx.id = txo.tx_id
        LEFT JOIN block ON tx.block_id = block.id
        LEFT JOIN datum ON txo.data_hash = datum.hash
        LEFT JOIN tx tx_in_ref ON txo.consumed_by_tx_id = tx_in_ref.id
        WHERE txo.payment_cred = ANY(%(addresses)b) AND txo.data_hash IS NOT NULL"""

        if out_tx_hash is not None:
            utxo_selector += """
        AND tx_in_ref.hash = ANY(%(out_tx_hash)b)"""
        elif in_tx_hash is not None:
            utxo_selector += """
        AND tx.hash = ANY(%(in_tx_hash)b)"""

        if block_no is not None:
            utxo_selector += """
        AND block.block_no = %(block_no)s"""
        elif after_block is not None:
            utxo_selector += """
        AND block.block_no >= %(after_block)s"""

        utxo_selector += """
    ) txo_stake
    LEFT JOIN (
        SELECT tx.hash AS "tx_hash",
        txo.index AS "tx_index",
        txo.value,
        txo.id as "tx_id",
        block.hash AS "block_hash",
        block.time AS "block_time",
        block.block_no,
        tx.block_index AS "block_index",
        tx_in.tx_id as "tx_out_id",
        tx_in.index as "tx_out_index",
        txo.inline_datum_id,
        txo.reference_script_id,
        txo.address,
        datum.hash as "datum_hash",
        datum.bytes as "datum_bytes"
        FROM tx_out tx_in
        LEFT JOIN tx ON tx.id = tx_in.consumed_by_tx_id
        LEFT JOIN tx_out txo ON tx.id = txo.tx_id
        LEFT JOIN block ON tx.block_id = block.id
        LEFT JOIN datum ON txo.data_hash = datum.hash
    ) txo_output ON txo_output.tx_out_id = txo_stake.tx_id
        AND txo_output.tx_out_index = txo_stake.index
    WHERE txo_stake.datum_hash IS NOT NULL
    ORDER BY txo_stake.tx_id ASC
    LIMIT %(limit)s
    OFFSET %(offset)s"""

        r = self.db_query(
            utxo_selector,
            {
                "addresses": [
                    Address.decode(a).payment_part.payload for a in stake_addresses
                ],
                "limit": limit,
                "offset": page * limit,
                "block_no": block_no,
                "after_block": after_block,
                "out_tx_hash": (
                    None
                    if out_tx_hash is None
                    else [bytes.fromhex(h) for h in out_tx_hash]
                ),
                "in_tx_hash": (
                    None
                    if in_tx_hash is None
                    else [bytes.fromhex(h) for h in in_tx_hash]
                ),
            },
        )

        return OrderSelector.parse(r)

    def get_cancel_utxos(
        self,
        stake_addresses: list[str],
        block_no: int | None = None,
        after_time: datetime | int | None = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get order cancel UTxOs."""
        if isinstance(after_time, int):
            after_time = datetime.fromtimestamp(after_time)

        utxo_selector = """
SELECT (
    SELECT array_agg(DISTINCT tx_out.address)
    FROM tx_out
    WHERE tx_out.consumed_by_tx_id = txo.tx_id
) AS "submit_address_inputs",
txo.address as "submit_address_stake",
ENCODE(tx.hash, 'hex') as "submit_tx_hash",
txo.index as "submit_tx_index",
ENCODE(block.hash,'hex') as "submit_block_hash",
EXTRACT(
    epoch
    FROM block.time
)::INTEGER AS "submit_block_time",
tx.block_index AS "submit_block_index",
(
    SELECT array_agg(tx_metadata.json)
    FROM tx_metadata
    WHERE txo.tx_id = tx_metadata.tx_id
) AS "submit_metadata",
COALESCE(
    json_build_object('lovelace',txo.value::TEXT)::jsonb || (
        SELECT json_agg(
            json_build_object(
                CONCAT(encode(ma.policy, 'hex'), encode(ma.name, 'hex')),
                mto.quantity::TEXT
            )
        )
        FROM ma_tx_out mto
        JOIN multi_asset ma ON (mto.ident = ma.id)
        WHERE mto.tx_out_id = txo.id
    )::jsonb,
    jsonb_build_array(json_build_object('lovelace',txo.value::TEXT)::jsonb)
) AS "submit_assets",
ENCODE(datum.hash,'hex') as "submit_datum_hash",
ENCODE(datum.bytes,'hex') as "submit_datum_cbor",
txo_output.address,
ENCODE(txo_output.tx_hash, 'hex') as "tx_hash",
txo_output.tx_index as "tx_index",
EXTRACT(
    epoch
    FROM txo_output.block_time
)::INTEGER AS "block_time",
txo_output.block_index AS "block_index",
ENCODE(txo_output.block_hash,'hex') AS "block_hash",
ENCODE(txo_output.datum_hash, 'hex') AS "datum_hash",
ENCODE(txo_output.datum_bytes, 'hex') AS "datum_cbor",
COALESCE(
    json_build_object('lovelace',txo_output.value::TEXT)::jsonb || (
        SELECT json_agg(
            json_build_object(
                CONCAT(encode(ma.policy, 'hex'), encode(ma.name, 'hex')),
                mto.quantity::TEXT
            )
        )
        FROM ma_tx_out mto
        JOIN multi_asset ma ON (mto.ident = ma.id)
        WHERE mto.tx_out_id = txo_output.tx_id
    )::jsonb,
    jsonb_build_array(json_build_object('lovelace',txo_output.value::TEXT)::jsonb)
) AS "assets",
(txo_output.inline_datum_id IS NOT NULL OR txo_output.reference_script_id IS NOT NULL)
    AS "plutus_v2"
"""

        utxo_selector += """FROM (
    SELECT tx.hash AS "tx_hash",
    txo.index AS "tx_index",
    txo.value,
    txo.id as "tx_id",
    block.hash AS "block_hash",
    block.time AS "block_time",
    block.block_no,
    tx.block_index AS "block_index",
    tx_in.tx_id as "tx_out_id",
    tx_in.index as "tx_out_index",
    txo.inline_datum_id,
    txo.reference_script_id,
    txo.address,
    datum.hash as "datum_hash",
    datum.bytes as "datum_bytes"
    FROM tx_out tx_in
    LEFT JOIN tx ON tx.id = tx_in.tx_id
    LEFT JOIN tx_out txo ON tx.id = txo.tx_id
    LEFT JOIN block ON tx.block_id = block.id
    LEFT JOIN datum ON txo.data_hash = datum.hash"""

        if after_time is not None:
            utxo_selector += """
    WHERE block.time >= %(after_time)s"""
        elif block_no is not None:
            utxo_selector += """
    WHERE block.block_no = %(block_no)s"""
        else:
            raise ValueError("Either after_time or block_no should be defined.")

        utxo_selector += """
    GROUP BY tx.hash, txo.value, txo.id, block.hash, block.time, block.block_no,
    tx.block_index, tx_in.tx_id, tx_in.index, txo.inline_datum_id,
    txo.reference_script_id, txo.address, datum.hash, datum.bytes
    HAVING COUNT(DISTINCT txo.address) = 1
) txo_output
LEFT JOIN tx_out txo ON txo.tx_id = txo_output.tx_out_id
    AND txo_output.tx_out_index = txo.index
    AND txo.payment_cred = ANY(%(addresses)b)
    AND txo.data_hash IS NOT NULL
LEFT JOIN tx ON tx.id = txo.tx_id
LEFT JOIN block ON tx.block_id = block.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN tx tx_in_ref ON txo.tx_id = tx_in_ref.id
WHERE txo.id IS NOT NULL
ORDER BY txo.tx_id ASC
LIMIT %(limit)s
OFFSET %(offset)s"""

        r = self.db_query(
            utxo_selector,
            {
                "addresses": [
                    Address.decode(a).payment_part.payload for a in stake_addresses
                ],
                "limit": limit,
                "offset": page * limit,
                "after_time": (
                    None
                    if after_time is None
                    else after_time.strftime("%Y-%m-%d %H:%M:%S")
                ),
                "block_no": block_no,
            },
        )

        return SwapTransactionList.model_validate(r)

    def get_axo_target(
        self,
        assets: Assets,
        block_time: datetime | None = None,
    ) -> str | None:
        """Get the target address for the given asset."""
        query = """
    SELECT DISTINCT txo.address, block.time
    FROM (
        SELECT tx.id, tx.block_id
        FROM tx_out txo
        LEFT JOIN tx ON tx.id = txo.tx_id
        WHERE txo.payment_cred = DECODE(
            '55ff0e63efa0694e8065122c552e80c7b51768b7f20917af25752a7c', 'hex'
        )
    ) as tx
    LEFT JOIN block ON block.id = tx.block_id
    LEFT JOIN tx_out txo ON tx.id = txo.tx_id
    LEFT JOIN ma_tx_out mtxo on txo.id = mtxo.tx_out_id
    LEFT JOIN multi_asset ma ON ma.id = mtxo.ident
    WHERE ma.policy = %(policy)b AND ma.name = %(name)b
    AND txo.payment_cred != DECODE(
        '55ff0e63efa0694e8065122c552e80c7b51768b7f20917af25752a7c', 'hex'
    )"""

        if block_time is not None:
            query += """
    AND block.time <= %(block_time)s"""

        query += """
    ORDER BY block.time DESC"""

        policy = bytes.fromhex(assets.unit()[:56])
        name = bytes.fromhex(assets.unit()[56:])
        r = self.db_query(
            query,
            {
                "policy": policy,
                "name": name,
                "block_time": (
                    None
                    if block_time is None
                    else block_time.strftime("%Y-%m-%d %H:%M:%S")
                ),
            },
        )

        if len(r) == 0:
            return None

        return r[0]["address"]
