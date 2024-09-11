"""BlockFrostBackend class for interacting with the BlockFrost API."""

import functools
from datetime import datetime
from typing import Optional
from typing import Union

from blockfrost import ApiUrls  # type: ignore
from pycardano import Address  # type: ignore
from pycardano import BlockFrostChainContext  # type: ignore

from charli3_dendrite.backend.backend_base import AbstractBackend
from charli3_dendrite.backend.blockfrost.models import AssetAmount
from charli3_dendrite.backend.blockfrost.models import BlockFrostBlockInfo
from charli3_dendrite.backend.blockfrost.models import TransactionInfo
from charli3_dendrite.backend.blockfrost.models import UTxO
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import BlockList
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dataclasses.models import SwapTransactionList


class BlockFrostBackend(AbstractBackend):
    """BlockFrostBackend class for interacting with the BlockFrost API."""

    def __init__(self, project_id: str) -> None:
        """Initialize the BlockFrostBackend.

        Args:
            project_id (str): The BlockFrost project ID.
        """
        self.chain_context = BlockFrostChainContext(
            project_id,
            base_url=ApiUrls.mainnet.value,
        )
        self.api = self.chain_context.api
        self._block_cache: dict = {}

    @functools.lru_cache(maxsize=100)  # noqa: B019
    def _get_block_info(self, block_hash: str) -> BlockFrostBlockInfo:
        """Get block information from cache or API.

        Args:
            block_hash (str): The hash of the block.

        Returns:
            dict: Block information.
        """
        if block_hash not in self._block_cache:
            block_info = self.api.block(block_hash)
            self._block_cache[block_hash] = BlockFrostBlockInfo(
                time=block_info.time,
                height=block_info.height,
            )
        return self._block_cache[block_hash]

    def get_pool_utxos(
        self,
        addresses: list[str],
        assets: Optional[list[str]] = None,
        limit: int = 1000,
        page: int = 0,
        historical: bool = True,
    ) -> PoolStateList:
        """Get pool UTXOs based on assets and addresses.

        Args:
            assets (Optional[list[str]]): list of asset IDs.
            addresses (Optional[list[str]]): list of addresses.
            limit (int): Maximum number of UTXOs to return.
            page (int): Page number for pagination.
            historical (bool): Include historical data.

        Returns:
            PoolStateList: list of pool states.
        """
        pool_states = []
        if addresses is None:
            return PoolStateList(root=[])

        for address in addresses:
            if assets:
                utxos = []
                for asset in assets:
                    utxos.extend(
                        self.api.address_utxos_asset(address, asset, gather_pages=True),
                    )
            else:
                utxos = self.api.address_utxos(address, gather_pages=True)

            for utxo in utxos[:limit]:
                if utxo.data_hash:
                    pool_state = self._utxo_to_pool_state(utxo)
                    if assets:
                        if any(asset in pool_state.assets for asset in assets):
                            pool_states.append(pool_state)
                    else:
                        pool_states.append(pool_state)
        return PoolStateList(root=pool_states)

    def get_pool_in_tx(
        self,
        tx_hash: str,
        addresses: list[str],
        assets: Optional[list[str]] = None,
    ) -> PoolStateList:
        """Get pool states for a specific transaction.

        Args:
            tx_hash (str): Transaction hash.
            assets (Optional[list[str]]): list of asset IDs.
            addresses (Optional[list[str]]): list of addresses.

        Returns:
            PoolStateList: list of pool states.
        """
        pool_states = []
        tx_utxos = self.api.transaction_utxos(tx_hash)
        for utxo in tx_utxos.outputs:
            if addresses and utxo.address not in addresses:
                continue
            pool_state = self._utxo_to_pool_state(utxo, tx_hash)
            if assets:
                if any(asset in pool_state.assets for asset in assets):
                    pool_states.append(pool_state)
            else:
                pool_states.append(pool_state)
        return PoolStateList(root=pool_states)

    def last_block(self, last_n_blocks: int = 2) -> BlockList:
        """Get information about the last N blocks.

        Args:
            last_n_blocks (int): Number of recent blocks to retrieve.

        Returns:
            BlockList: list of recent block information.
        """
        blocks = []
        latest_block = self.api.block_latest()
        for i in range(last_n_blocks):
            block = self.api.block(latest_block.height - i)
            blocks.append(
                {
                    "epoch_slot_no": block.epoch_slot,
                    "block_no": block.height,
                    "tx_count": block.tx_count,
                    "block_time": block.time,
                },
            )
        return BlockList(root=blocks)

    def get_pool_utxos_in_block(self, block_no: int) -> PoolStateList:
        """Get pool UTXOs for a specific block.

        Args:
            block_no (int): Block number.

        Returns:
            PoolStateList: list of pool states.
        """
        tx_hashes = self.api.block_transactions(block_no)
        pool_states = []
        for tx_hash in tx_hashes:
            tx_utxos = self.api.transaction_utxos(tx_hash)
            for utxo in tx_utxos.outputs:
                if utxo.data_hash:  # Only consider UTXOs with datums
                    pool_states.append(self._utxo_to_pool_state(utxo, tx_hash))
        return PoolStateList(root=pool_states)

    def get_script_from_address(self, address: Address) -> ScriptReference:
        """Get script reference for a given address.

        Args:
            address (Address): The address to query.

        Returns:
            ScriptReference: Script reference for the address.
        """
        script_hash = address.payment_part.payload.hex()
        script = self.api.script_cbor(script_hash)
        return ScriptReference(
            tx_hash=None,
            tx_index=None,
            address=str(address),
            assets=None,
            datum_hash=None,
            datum_cbor=None,
            script=script.cbor,
        )

    def get_historical_order_utxos(
        self,
        stake_addresses: list[str],
        after_time: Optional[Union[datetime, int]] = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get historical order UTXOs.

        This method is not supported due to limited data availability.

        Raises:
            NotImplementedError: This method is not implemented.
        """
        raise NotImplementedError(
            "This method is not supported due to limited data availability",
        )

    def get_order_utxos_by_block_or_tx(
        self,
        stake_addresses: list[str],
        out_tx_hash: Optional[list[str]] = None,
        in_tx_hash: Optional[list[str]] = None,
        block_no: Optional[int] = None,
        after_block: Optional[int] = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get order UTXOs by block or transaction.

        This method is not supported due to limited data availability.

        Raises:
            NotImplementedError: This method is not implemented.
        """
        raise NotImplementedError(
            "This method is not supported due to limited data availability",
        )

    def get_cancel_utxos(
        self,
        stake_addresses: list[str],
        block_no: Optional[int] = None,
        after_time: Optional[Union[datetime, int]] = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get cancelled order UTXOs.

        This method is not supported due to limited data availability.

        Raises:
            NotImplementedError: This method is not implemented.
        """
        raise NotImplementedError(
            "This method is not supported due to limited data availability",
        )

    def get_datum_from_address(
        self,
        address: str,
        asset: Optional[str] = None,
    ) -> Optional[ScriptReference]:
        """Get datum from a given address.

        Args:
            address (str): The address to query.
            asset (Optional[str]): Asset to filter by.

        Returns:
            Optional[ScriptReference]: The datum associated with the address, if any.
        """
        utxos = self.api.address_utxos(address, gather_pages=True)
        for utxo in utxos:
            if asset and asset not in utxo.amount:
                continue
            if utxo.data_hash:
                return ScriptReference(
                    tx_hash=utxo.tx_hash,
                    tx_index=utxo.output_index,
                    address=utxo.address,
                    assets=self._format_assets(utxo.amount),
                    datum_hash=utxo.data_hash,
                    datum_cbor=(
                        utxo.inline_datum
                        if utxo.inline_datum
                        else self._get_datum_from_datum_hash(utxo.data_hash)
                    ),
                    script=None,
                )
        return None

    def get_axo_target(
        self,
        assets: Assets,
        block_time: Optional[datetime] = None,
    ) -> Optional[str]:
        """Get the target address for the given asset.

        This method is not supported due to limited data availability.

        Raises:
            NotImplementedError: This method is not implemented.
        """
        raise NotImplementedError(
            "This method is not supported due to limited data availability",
        )

    def _utxo_to_pool_state(
        self,
        utxo: UTxO,
        tx_hash: Optional[str] = None,
    ) -> PoolStateInfo:
        """Convert UTXO to PoolStateInfo.

        Args:
            utxo: UTXO object.
            tx_hash (Optional[str]): Transaction hash.

        Returns:
            PoolStateInfo: Pool state information.
        """
        if tx_hash:
            tx_info = self.api.transaction(tx_hash)
        return PoolStateInfo(
            address=utxo.address,
            tx_hash=tx_hash if tx_hash else utxo.tx_hash,
            tx_index=utxo.output_index,
            block_time=tx_info.block_time if tx_hash else 0,
            block_index=tx_info.index if tx_hash else 0,
            block_hash=tx_info.block if tx_hash else utxo.block,
            datum_hash=utxo.data_hash or "",
            datum_cbor=(
                utxo.inline_datum
                if utxo.inline_datum
                else (
                    self._get_datum_from_datum_hash(utxo.data_hash)
                    if utxo.data_hash
                    else ""
                )
            ),
            assets=self._format_assets(utxo.amount),
            plutus_v2=utxo.reference_script_hash is not None,
        )

    def _format_assets(self, amount: list[AssetAmount]) -> Assets:
        """Format assets from BlockFrost format to Assets model.

        Args:
            amount: BlockFrost asset amount.

        Returns:
            Assets: Formatted assets.
        """
        formatted_assets = {}
        for asset in amount:
            if asset.unit == "lovelace":
                formatted_assets["lovelace"] = int(asset.quantity)
            else:
                formatted_assets[asset.unit] = int(asset.quantity)
        return Assets(root=formatted_assets)

    def _get_block_time(self, block_hash: str) -> int:
        """Get block time from block hash.

        You might want to cache this information to avoid repeated API calls.

        Args:
            block_hash (str): Block hash.

        Returns:
            int: Block time.
        """
        block_info = self.api.block(block_hash)
        return block_info.time

    def _get_block_index(self, block_hash: str) -> int:
        """Get block index from block hash.

        You might want to cache this information to avoid repeated API calls.

        Args:
            block_hash (str): Block hash.

        Returns:
            int: Block index.
        """
        block_info = self.api.block(block_hash)
        return block_info.height

    def _get_datum_from_datum_hash(self, datum_hash: str) -> str:
        """Get datum CBOR from datum hash.

        Args:
            datum_hash (str): Datum hash.

        Returns:
            str: Datum CBOR.
        """
        datum = self.api.script_datum_cbor(datum_hash)
        return datum.cbor
