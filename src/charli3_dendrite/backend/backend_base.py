"""Abstract base class for Cardano blockchain backend implementations."""

from abc import ABC
from abc import abstractmethod
from datetime import datetime
from typing import Optional

from pycardano import Address  # type: ignore

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import BlockList
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dataclasses.models import SwapTransactionList


class AbstractBackend(ABC):
    """Abstract base class for Cardano blockchain backend implementations.

    This class defines the interface for interacting with various Cardano blockchain
    data sources such as db-sync, Ogmios, or Kupo.
    """

    @abstractmethod
    def get_pool_utxos(
        self,
        addresses: list[str],
        assets: list[str] | None = None,
        limit: int = 1000,
        page: int = 0,
        historical: bool = True,
    ) -> PoolStateList:
        """Get UTXOs for specific assets or addresses.

        Args:
            assets (Optional[List[str]]): List of asset IDs to filter by.
            addresses (Optional[List[str]]): List of addresses to filter by.
            limit (int): Maximum number of results to return.
            page (int): Page number for pagination.
            historical (bool): Whether to include historical data.

        Returns:
            PoolStateList: List of pool state objects.
        """
        pass

    @abstractmethod
    def get_pool_in_tx(
        self,
        tx_hash: str,
        addresses: list[str],
        assets: list[str] | None = None,
    ) -> PoolStateList:
        """Get pool state for a specific transaction.

        Args:
            tx_hash (str): The transaction hash to query.
            assets (Optional[List[str]]): List of asset IDs to filter by.
            addresses (Optional[List[str]]): List of addresses to filter by.

        Returns:
            PoolStateList: List of pool state objects for the transaction.
        """
        pass

    @abstractmethod
    def last_block(self, last_n_blocks: int = 2) -> BlockList:
        """Get information about the last n blocks.

        Args:
            last_n_blocks (int): Number of recent blocks to retrieve.

        Returns:
            BlockList: List of recent block information.
        """
        pass

    @abstractmethod
    def get_pool_utxos_in_block(self, block_no: int) -> PoolStateList:
        """Get pool UTXOs for a specific block.

        Args:
            block_no (int): The block number to query.

        Returns:
            PoolStateList: List of pool state objects for the block.
        """
        pass

    @abstractmethod
    def get_script_from_address(self, address: Address) -> ScriptReference:
        """Get script reference for a given address.

        Args:
            address (Address): The address to query.

        Returns:
            ScriptReference: Script reference for the address.
        """
        pass

    @abstractmethod
    def get_historical_order_utxos(
        self,
        stake_addresses: list[str],
        after_time: datetime | int | None = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get historical order UTXOs for given stake addresses.

        Args:
            stake_addresses (List[str]): List of stake addresses to query.
            after_time (Optional[Union[datetime, int]]): Filter results after this time.
            limit (int): Maximum number of results to return.
            page (int): Page number for pagination.

        Returns:
            SwapTransactionList: List of swap transaction objects.
        """
        pass

    @abstractmethod
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
        """Get order UTXOs by block or transaction.

        Args:
            stake_addresses (List[str]): List of stake addresses to query.
            out_tx_hash (Optional[List[str]]): List of transaction hashes to filter by.
            in_tx_hash: list of input transaction hashes to filter by.
            block_no (Optional[int]): Specific block number to query.
            after_block (Optional[int]): Filter results after this block number.
            limit (int): Maximum number of results to return.
            page (int): Page number for pagination.

        Returns:
            SwapTransactionList: List of swap transaction objects.
        """
        pass

    @abstractmethod
    def get_cancel_utxos(
        self,
        stake_addresses: list[str],
        block_no: int | None = None,
        after_time: datetime | int | None = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get cancelled order UTXOs.

        Args:
            stake_addresses (List[str]): List of stake addresses to query.
            block_no (Optional[int]): Specific block number to query.
            after_time (Optional[Union[datetime, int]]): Filter results after this time.
            limit (int): Maximum number of results to return.
            page (int): Page number for pagination.

        Returns:
            SwapTransactionList: List of swap transaction objects for cancelled orders.
        """
        pass

    @abstractmethod
    def get_datum_from_address(
        self,
        address: str,
        asset: str | None = None,
    ) -> Optional[ScriptReference]:
        """Get datum from a given address.

        Args:
            address: The address to query.
            asset: Assets required to be in the UTxO.

        Returns:
            The datum associated with the address, if any.
        """
        pass

    @abstractmethod
    def get_axo_target(
        self,
        assets: Assets,
        block_time: datetime | None = None,
    ) -> str | None:
        """Get the target address for the given assets.

        Args:
            assets: The assets to query.
            block_time: The block time to query.

        Returns:
            The target address for the assets, if any.
        """
        pass
