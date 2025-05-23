"""Methods for abstracting SQL selections and parsing results."""

from abc import ABC
from abc import abstractmethod

from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dataclasses.models import SwapTransactionList


class AbstractDBSyncStructure(ABC):
    """Abstract class with required methods for SQL queries."""

    @classmethod
    @abstractmethod
    def select(cls) -> str:
        """The selectin part of a DBSync query."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def parse(cls, data: dict | list[dict]) -> DendriteBaseModel:
        """Parse data returned from a dbsync query."""
        raise NotImplementedError


class PoolSelector(AbstractDBSyncStructure):
    """Query selections and parsing classes for AMM pools."""

    @classmethod
    def select(cls) -> str:
        """Select SQL query for swap pools."""
        return """
SELECT txo.address,
ENCODE(tx.hash, 'hex') as "tx_hash",
txo.index as "tx_index",
EXTRACT(
	epoch
	FROM block.time
)::INTEGER AS "block_time",
tx.block_index as "block_index",
ENCODE(block.hash,'hex') as "block_hash",
ENCODE(datum.hash,'hex') as "datum_hash",
ENCODE(datum.bytes,'hex') as "datum_cbor",
COALESCE (
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
) AS "assets",
(txo.inline_datum_id IS NOT NULL OR txo.reference_script_id IS NOT NULL) as "plutus_v2"
"""

    @classmethod
    def parse(cls, data: dict | list[dict]) -> PoolStateList:
        """Parse pools from a query."""
        return PoolStateList.model_validate(data)


class UTxOSelector(AbstractDBSyncStructure):
    """Selection queries and parsing classes for reference scripts and datums."""

    @classmethod
    def select(cls) -> str:
        """Selection SQL query for reference scripts and datums."""
        return """
SELECT ENCODE(tx.hash, 'hex') as "tx_hash",
tx_out.index as "tx_index",
address.address,
ENCODE(datum.hash,'hex') as "datum_hash",
ENCODE(datum.bytes,'hex') as "datum_cbor",
COALESCE (
    json_build_object('lovelace',tx_out.value::TEXT)::jsonb || (
        SELECT json_agg(
            json_build_object(
                CONCAT(encode(ma.policy, 'hex'), encode(ma.name, 'hex')),
                mto.quantity::TEXT
            )
        )
        FROM ma_tx_out mto
        JOIN multi_asset ma ON (mto.ident = ma.id)
        WHERE mto.tx_out_id = tx_out.id
    )::jsonb,
    jsonb_build_array(json_build_object('lovelace',tx_out.value::TEXT)::jsonb)
) AS "assets",
ENCODE(s.bytes, 'hex') as "script" """

    @classmethod
    def parse(cls, data: dict | list[dict]) -> ScriptReference:
        """Parsing class for UTxOs containing a reference script or datum."""
        return ScriptReference.model_validate(data)


class OrderSelector(AbstractDBSyncStructure):
    """SQL query for orders and the associated pydantic parsing class."""

    @classmethod
    def select(cls) -> str:
        """The SQL select statement for orders."""
        return """
SELECT (
	SELECT array_agg(DISTINCT address.address)
	FROM tx_out txo
	WHERE txo.consumed_by_tx_id = txo_stake.tx_id
) AS "submit_address_inputs",
address.address as "submit_address_stake",
ENCODE(tx.hash, 'hex') as "submit_tx_hash",
txo_stake.index as "submit_tx_index",
ENCODE(block.hash,'hex') as "submit_block_hash",
EXTRACT(
	epoch
	FROM block.time
)::INTEGER AS "submit_block_time",
tx.block_index AS "submit_block_index",
(
	SELECT array_agg(tx_metadata.json)
	FROM tx_metadata
	WHERE tx.id = tx_metadata.tx_id
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
(
    txo_output.inline_datum_id IS NOT NULL OR txo_output.reference_script_id IS NOT NULL
) as "plutus_v2"
"""

    @classmethod
    def parse(cls, data: dict | list[dict]) -> SwapTransactionList:
        """Parse and validate orders."""
        return SwapTransactionList.model_validate(data)
