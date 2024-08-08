from datetime import datetime

from pycardano import Address

from cardex.backend.dbsync.models import OrderSelector
from cardex.backend.dbsync.utils import db_query
from cardex.dataclasses.models import SwapTransactionList


def get_historical_order_utxos(
    stake_addresses: list[str],
    after_time: datetime | int | None = None,
    limit: int = 1000,
    page: int = 0,
):
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
	tx_in.tx_out_id,
	tx_in.tx_out_index,
    txo.inline_datum_id,
    txo.reference_script_id,
    txo.address,
    datum.hash as "datum_hash",
    datum.bytes as "datum_bytes"
	FROM tx_in
	LEFT JOIN tx ON tx.id = tx_in.tx_in_id
	LEFT JOIN tx_out txo ON tx.id = txo.tx_id
	LEFT JOIN block ON tx.block_id = block.id
	LEFT JOIN datum ON txo.data_hash = datum.hash
) txo_output ON txo_output.tx_out_id = txo_stake.tx_id AND txo_output.tx_out_index = txo_stake.index
WHERE datum.hash IS NOT NULL"""

    if after_time is not None:
        utxo_selector += """
    AND block.time >= %(after_time)s"""

    utxo_selector += """
ORDER BY tx.id ASC
LIMIT %(limit)s
OFFSET %(offset)s"""

    r = db_query(
        utxo_selector,
        {
            "addresses": [
                Address.decode(a).payment_part.payload for a in stake_addresses
            ],
            "limit": limit,
            "offset": page * limit,
            "after_time": None
            if after_time is None
            else after_time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )

    return OrderSelector.parse(r)


def get_cancel_utxos(
    stake_addresses: list[str],
    block_no: int | None = None,
    after_time: datetime | int | None = None,
    limit: int = 1000,
    page: int = 0,
):
    if isinstance(after_time, int):
        after_time = datetime.fromtimestamp(after_time)

    utxo_selector = """
SELECT (
	SELECT array_agg(DISTINCT tx_out.address)
	FROM tx_out
	LEFT JOIN tx_in txi ON tx_out.tx_id = txi.tx_out_id AND tx_out.index = txi.tx_out_index
	WHERE txi.tx_in_id = txo.tx_id
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
(txo_output.inline_datum_id IS NOT NULL OR txo_output.reference_script_id IS NOT NULL) as "plutus_v2"
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
	tx_in.tx_out_id,
	tx_in.tx_out_index,
    txo.inline_datum_id,
    txo.reference_script_id,
    txo.address,
    datum.hash as "datum_hash",
    datum.bytes as "datum_bytes"
	FROM tx_in
	LEFT JOIN tx ON tx.id = tx_in.tx_in_id
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
    tx.block_index, tx_in.tx_out_id, tx_in.tx_out_index, txo.inline_datum_id, txo.reference_script_id,
    txo.address, datum.hash, datum.bytes
	HAVING COUNT(DISTINCT txo.address) = 1
) txo_output
LEFT JOIN tx_out txo ON txo.tx_id = txo_output.tx_out_id
    AND txo_output.tx_out_index = txo.index
	AND txo.payment_cred = ANY(%(addresses)b)
    AND txo.data_hash IS NOT NULL
LEFT JOIN tx ON tx.id = txo.tx_id
LEFT JOIN block ON tx.block_id = block.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN tx_in ON tx_in.tx_out_id = tx.id AND tx_in.tx_out_index = txo.index
LEFT JOIN tx tx_in_ref ON tx_in.tx_in_id = tx_in_ref.id
WHERE txo.id IS NOT NULL
ORDER BY txo.tx_id ASC
LIMIT %(limit)s
OFFSET %(offset)s"""

    r = db_query(
        utxo_selector,
        {
            "addresses": [
                Address.decode(a).payment_part.payload for a in stake_addresses
            ],
            "limit": limit,
            "offset": page * limit,
            "after_time": None
            if after_time is None
            else after_time.strftime("%Y-%m-%d %H:%M:%S"),
            "block_no": block_no,
        },
    )

    return OrderSelector.parse(r)


def get_order_utxos_by_block_or_tx(
    stake_addresses: list[str],
    out_tx_hash: list[str] | None = None,
    in_tx_hash: list[str] | None = None,
    block_no: int | None = None,
    after_block: int | None = None,
    limit: int = 1000,
    page: int = 0,
) -> SwapTransactionList:
    utxo_selector = """
SELECT (
	SELECT array_agg(DISTINCT txo.address)
	FROM tx_out txo
	LEFT JOIN tx_in txi ON txo.tx_id = txi.tx_out_id AND txo.index = txi.tx_out_index
	WHERE txi.tx_in_id = txo_stake.tx_id
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
(txo_output.inline_datum_id IS NOT NULL OR txo_output.reference_script_id IS NOT NULL) as "plutus_v2"
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
	LEFT JOIN tx_in ON tx_in.tx_out_id = tx.id AND tx_in.tx_out_index = txo.index
	LEFT JOIN tx tx_in_ref ON tx_in.tx_in_id = tx_in_ref.id
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
	tx_in.tx_out_id,
	tx_in.tx_out_index,
    txo.inline_datum_id,
    txo.reference_script_id,
    txo.address,
    datum.hash as "datum_hash",
    datum.bytes as "datum_bytes"
	FROM tx_in
	LEFT JOIN tx ON tx.id = tx_in.tx_in_id
	LEFT JOIN tx_out txo ON tx.id = txo.tx_id
	LEFT JOIN block ON tx.block_id = block.id
	LEFT JOIN datum ON txo.data_hash = datum.hash
) txo_output ON txo_output.tx_out_id = txo_stake.tx_id AND txo_output.tx_out_index = txo_stake.index
WHERE txo_stake.datum_hash IS NOT NULL
ORDER BY txo_stake.tx_id ASC
LIMIT %(limit)s
OFFSET %(offset)s"""

    r = db_query(
        utxo_selector,
        {
            "addresses": [
                Address.decode(a).payment_part.payload for a in stake_addresses
            ],
            "limit": limit,
            "offset": page * limit,
            "block_no": block_no,
            "after_block": after_block,
            "out_tx_hash": None
            if out_tx_hash is None
            else [bytes.fromhex(h) for h in out_tx_hash],
            "in_tx_hash": None
            if in_tx_hash is None
            else [bytes.fromhex(h) for h in in_tx_hash],
        },
    )

    return OrderSelector.parse(r)
