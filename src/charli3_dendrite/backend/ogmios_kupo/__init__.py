"""Backend that uses Ogmios for chain context and Kupo for pool state information."""

import logging
from datetime import datetime
from typing import Optional
from typing import Union

import requests
from pycardano import Address  # type: ignore
from pycardano import KupoOgmiosV6ChainContext
from pycardano import Network  # type: ignore

from charli3_dendrite.backend.backend_base import AbstractBackend
from charli3_dendrite.backend.ogmios_kupo.models import AssetValue
from charli3_dendrite.backend.ogmios_kupo.models import KupoDatumResponse
from charli3_dendrite.backend.ogmios_kupo.models import KupoGenericResponse
from charli3_dendrite.backend.ogmios_kupo.models import KupoResponse
from charli3_dendrite.backend.ogmios_kupo.models import KupoResponseList
from charli3_dendrite.backend.ogmios_kupo.models import KupoScriptResponse
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import BlockInfo
from charli3_dendrite.dataclasses.models import BlockList
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dataclasses.models import SwapTransactionList

SHELLEY_START = 1596491091
SLOT_LENGTH = 1
SHELLEY_SLOT_OFFSET = 4924800
POLICY_ID_LENGTH: int = 56
AXO_PAYMENT_CREDENTIAL = "55ff0e63efa0694e8065122c552e80c7b51768b7f20917af25752a7c"

logger = logging.getLogger(__name__)


class OgmiosKupoBackend(AbstractBackend):
    """Backend class for fetching pool state information from Ogmios and Kupo."""

    def __init__(
        self,
        ogmios_url: str,
        kupo_url: str,
        network: Network,
    ) -> None:
        """Initialize the OgmiosKupoBackend.

        Args:
            ogmios_url (str): URL for the Ogmios service.
            kupo_url (str): URL for the Kupo service.
            network (Network): The Cardano network to use.
        """
        _, ws_string = ogmios_url.split("ws://")
        self.ws_url, self.port = ws_string.split(":")
        self.ogmios_context = KupoOgmiosV6ChainContext(
            host=self.ws_url,
            port=int(self.port),
            secure=False,
            refetch_chain_tip_interval=None,
            network=network,
            kupo_url=kupo_url,
        )

    def _kupo_request(
        self,
        endpoint: str,
        params: Optional[dict] = None,
    ) -> KupoGenericResponse:
        """Make a request to the Kupo API.

        Args:
            endpoint (str): The API endpoint to request.
            params (Optional[dict]): Query parameters for the request.

        Returns:
            KupoGenericResponse: The JSON response from the API.

        Raises:
            requests.exceptions.RequestException: If the request fails.
        """
        url = f"{self.ogmios_context._kupo_url}/{endpoint}"
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return KupoGenericResponse.model_validate(response.json())

    def _pool_state_from_kupo(self, match_data: KupoResponse) -> PoolStateInfo:
        """Convert Kupo match data to PoolStateInfo.

        Args:
            match_data (KupoResponse): The Kupo match data.

        Returns:
            PoolStateInfo: The converted pool state information.
        """
        assets = self._format_assets(match_data.value)

        current_slot = match_data.created_at.slot_no
        block_time = SHELLEY_START + current_slot - SHELLEY_SLOT_OFFSET

        datum_cbor = (
            self._get_datum_cbor(match_data.datum_hash) if match_data.datum_hash else ""
        )
        return PoolStateInfo(
            address=match_data.address,
            tx_hash=match_data.transaction_id,
            tx_index=match_data.output_index,
            block_time=block_time,
            block_index=match_data.transaction_index,
            block_hash=match_data.created_at.header_hash,
            datum_hash=match_data.datum_hash,
            datum_cbor=datum_cbor,
            assets=assets,
            plutus_v2=match_data.script_hash is not None,
        )

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
            addresses (list[str]): List of addresses to query.
            assets (Optional[list[str]]): List of asset IDs to filter by.
            limit (int): Maximum number of UTXOs to return.
            page (int): Page number for pagination.
            historical (bool): Whether to include historical data.

        Returns:
            PoolStateList: List of pool states.
        """
        pool_states = []
        if not addresses:
            return PoolStateList(root=[])

        for address in addresses:
            params: dict[str, Union[int, str, None]] = {
                "limit": limit,
                "offset": page * limit,
            }
            payment_cred = self.get_payment_credential(address)
            if assets:
                last_asset = assets[-1]
                params["policy_id"] = last_asset[:POLICY_ID_LENGTH]
                params["asset_name"] = (
                    last_asset[POLICY_ID_LENGTH:]
                    if len(last_asset) > POLICY_ID_LENGTH
                    else None
                )

            matches = self._kupo_request(
                f"matches/{payment_cred}/*?unspent",
                params=params,
            )
            if isinstance(matches.root, list):
                for match in matches.root:
                    pool_state = self._pool_state_from_kupo(match)
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
            tx_hash (str): The transaction hash to query.
            assets (Optional[list[str]]): List of asset IDs to filter by.
            addresses (Optional[list[str]]): List of addresses to query.

        Returns:
            PoolStateList: List of pool states for the transaction.
        """
        pool_states: list[PoolStateInfo] = []
        if addresses is None:
            return PoolStateList(root=[])

        for address in addresses:
            params: dict[str, Union[int, str, None]] = {
                "transaction_id": tx_hash,
                "order": "most_recent_first",
            }
            payment_cred = self.get_payment_credential(address)
            if assets:
                last_asset = assets[-1]
                params["policy_id"] = last_asset[:POLICY_ID_LENGTH]
                params["asset_name"] = (
                    last_asset[POLICY_ID_LENGTH:]
                    if len(last_asset) > POLICY_ID_LENGTH
                    else None
                )

            matches = self._kupo_request(f"matches/{payment_cred}/*", params=params)
            if isinstance(matches.root, list):
                pool_states = []
                if matches.root:
                    for match in matches.root:
                        pool_state = self._pool_state_from_kupo(match)
                        pool_states.append(pool_state)
                else:
                    return PoolStateList(root=[])

        return PoolStateList(root=pool_states)

    def last_block(self, last_n_blocks: int = 2) -> BlockList:
        """Get information about the last n blocks.

        Args:
            last_n_blocks (int): Number of recent blocks to retrieve.

        Returns:
            BlockList: List of recent block information.
        """
        ogmios_health = self._ogmios_request("health")
        latest_slot = ogmios_health["lastKnownTip"]["slot"]
        latest_block_no = ogmios_health["lastKnownTip"]["height"]
        current_epoch_slot = ogmios_health["slotInEpoch"]

        # Calculate the range for Kupo query
        created_before = latest_slot + 1
        created_after = latest_slot - (last_n_blocks * 20)

        # Query Kupo for UTXOs created during the time period
        kupo_matches = self._kupo_request(
            "matches",
            params={
                "created_after": created_after,
                "created_before": created_before,
            },
        )

        block_data = {}  # type: ignore
        for match in kupo_matches.root:
            header_hash = match.created_at.header_hash
            slot_no = match.created_at.slot_no
            tx_hash = match.transaction_id
            if header_hash not in block_data:
                block_data[header_hash] = {"slot_no": slot_no, "tx_hashes": set()}
            block_data[header_hash]["tx_hashes"].add(tx_hash)

        sorted_blocks = sorted(
            block_data.items(),
            key=lambda x: x[1]["slot_no"],
            reverse=True,
        )[:last_n_blocks]

        blocks = []
        for i, (_, block_info) in enumerate(sorted_blocks):
            current_slot = block_info["slot_no"]
            current_block_no = latest_block_no - i

            slot_diff = latest_slot - current_slot
            epoch_slot = (current_epoch_slot - slot_diff) % 432000

            block_time = SHELLEY_START + current_slot - SHELLEY_SLOT_OFFSET

            blocks.append(
                BlockInfo(
                    epoch_slot_no=epoch_slot,
                    block_no=current_block_no,
                    tx_count=len(block_info["tx_hashes"]),
                    block_time=block_time,
                ),
            )

        return BlockList(root=blocks)

    def _ogmios_request(self, endpoint: str) -> dict:
        """Make a request to the Ogmios API.

        Args:
            endpoint (str): The API endpoint to request.

        Returns:
            dict: The JSON response from the API.

        Raises:
            requests.exceptions.RequestException: If the request fails.
        """
        url = f"http://{self.ws_url}:{self.port}/{endpoint}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()

    def get_pool_utxos_in_block(self, block_no: int, block_time: int) -> PoolStateList:  # type: ignore
        """Get pool UTXOs created in a specific block.

        Args:
            block_no (int): The block number to query.
            block_time (int): The block time.

        Returns:
            PoolStateList: List of pool state objects for the block.

        Note: This method only works with blocktime as input, not block number.
        """
        block_slot = block_time - SHELLEY_START + 4924800
        params = {"created_after": block_slot, "order": "most_recent_first"}
        matches = self._kupo_request("matches", params=params)
        pool_states = []
        if isinstance(matches.root, list):
            for match in matches.root:
                if isinstance(match, KupoResponse) and match.datum_hash:
                    pool_state = self._pool_state_from_kupo(match)
                    pool_states.append(pool_state)
        return PoolStateList(root=pool_states)

    def get_script_from_address(self, address: Address) -> ScriptReference:
        """Get script reference for a given address.

        Args:
            address (Address): The address to query.

        Returns:
            ScriptReference: Script reference for the address.
        """
        script_hash = address.payment_part.payload.hex()
        response = self._kupo_request(f"scripts/{script_hash}")

        if isinstance(response.root, KupoScriptResponse):
            return ScriptReference(
                tx_hash=None,
                tx_index=None,
                address=str(address),
                assets=None,
                datum_hash=None,
                datum_cbor=None,
                script=response.root.script,
            )

        return ScriptReference(
            tx_hash=None,
            tx_index=None,
            address=str(address),
            assets=None,
            datum_hash=None,
            datum_cbor=None,
            script=None,
        )

    def _time_to_slot(self, time_value: Union[int, datetime]) -> int:
        """Convert a time value (Unix timestamp or datetime) to a slot number.

        Args:
            time_value (Union[int, datetime]): The time value to convert.

        Returns:
            int: The slot number.
        """
        if isinstance(time_value, datetime):
            unix_time = int(time_value.timestamp())
        elif isinstance(time_value, int):
            unix_time = time_value

        return ((unix_time - SHELLEY_START) // SLOT_LENGTH) + SHELLEY_SLOT_OFFSET

    def _slot_to_time(self, slot: int) -> int:
        """Convert a slot number to a Unix timestamp.

        Args:
            slot (int): The slot number to convert.

        Returns:
            int: The Unix timestamp.
        """
        return SHELLEY_START + (slot - SHELLEY_SLOT_OFFSET) * SLOT_LENGTH

    def get_historical_order_utxos(
        self,
        stake_addresses: list[str],
        after_time: Optional[Union[datetime, int]] = None,
        limit: int = 1000,
        page: int = 0,
    ) -> SwapTransactionList:
        """Get historical order UTXOs.

        This method is not supported due to limited data availability in Kupo.

        Raises:
            NotImplementedError: This method is not implemented.
        """
        raise NotImplementedError(
            "This method is not supported due to limited data availability in Kupo",
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

        This method is not supported due to limited data availability in Kupo.

        Raises:
            NotImplementedError: This method is not implemented.
        """
        raise NotImplementedError(
            "This method is not supported due to limited data availability in Kupo",
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

        This method is not supported due to limited data availability in Kupo.

        Raises:
            NotImplementedError: This method is not implemented.
        """
        raise NotImplementedError(
            "This method is not supported due to limited data availability in Kupo",
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
        params = {"unspent": None, "order": "most_recent_first"}
        if asset:
            params["policy_id"] = asset[:POLICY_ID_LENGTH]
            params["asset_name"] = (
                asset[POLICY_ID_LENGTH:] if len(asset) > POLICY_ID_LENGTH else None
            )

        matches: KupoGenericResponse = self._kupo_request(
            f"matches/{address}",
            params=params,
        )
        if isinstance(matches.root, list) and matches.root:
            match: KupoResponse = matches.root[0]
            datum_hash = match.datum_hash
            if datum_hash:
                datum_response: KupoGenericResponse = self._kupo_request(
                    f"datums/{datum_hash}",
                )
                if isinstance(datum_response.root, KupoDatumResponse):
                    assets = self._format_assets(match.value)
                    return ScriptReference(
                        tx_hash=match.transaction_id,
                        tx_index=match.output_index,
                        address=address.encode(),
                        assets=assets,
                        datum_hash=datum_hash,
                        datum_cbor=datum_response.root.datum,
                        script=None,
                    )
        return None

    def get_axo_target(
        self,
        assets: Assets,
        block_time: Optional[datetime] = None,
    ) -> Optional[str]:
        """Get the target address for the given asset."""
        # Extract policy and name from assets
        policy = assets.unit()[:POLICY_ID_LENGTH]
        name = assets.unit()[POLICY_ID_LENGTH:]
        asset_id = f"{policy}.{name}"

        # Prepare parameters for Kupo request
        params: dict[str, Union[str, int, None]] = {
            "policy_id": policy,
            "asset_name": name if name else None,
            "order": "most_recent_first",
        }

        if block_time:
            params["created_before"] = self._time_to_slot(block_time)

        try:
            # Query Kupo for matches
            matches = self._kupo_request(
                f"matches/{AXO_PAYMENT_CREDENTIAL}/*",
                params=params,
            )

            # Filter and process matches
            for match in matches.root:
                # Query all outputs for the transaction
                tx_outputs = self._kupo_request(f"matches/*@{match.transaction_id}")

                # Find the first output that contains the asset and is not an AXO addr
                for output in tx_outputs.root:
                    output_payment_cred = self.get_payment_credential(output.address)
                    if (
                        asset_id in output.value.assets
                        and output_payment_cred != AXO_PAYMENT_CREDENTIAL
                    ):
                        return output.address

            # If no suitable address found
            return None

        except requests.RequestException as e:
            logger.error("Error in get_axo_target: %s", e)
            return None

    def _format_assets(self, value: AssetValue) -> Assets:
        """Format assets from Kupo format to Assets model.

        Args:
            value (AssetValue): The asset value.

        Returns:
            Assets: Formatted assets.
        """
        formatted_assets = {"lovelace": value.coins}
        for asset_id, amount in value.assets.items():
            policy_id, asset_name = (
                asset_id.split(".", 1) if "." in asset_id else (asset_id, "")
            )
            formatted_asset_id = f"{policy_id}{asset_name}"
            formatted_assets[formatted_asset_id] = amount
        return Assets(root=formatted_assets)

    def _get_datum_cbor(self, datum_hash: Optional[str]) -> Optional[str]:
        """Get datum CBOR from datum hash.

        Args:
            datum_hash (Optional[str]): The datum hash.

        Returns:
            Optional[str]: The datum CBOR if found, None otherwise.
        """
        if datum_hash:
            try:
                datum_data = self._kupo_request(f"datums/{datum_hash}")
                if isinstance(datum_data.root, KupoDatumResponse):
                    return datum_data.root.datum
            except requests.RequestException as e:
                logger.error("Error fetching datum CBOR: %s", e)
        return None

    def get_payment_credential(self, cardano_address: str) -> str:
        """Get the payment credential from a Cardano address string.

        Args:
            cardano_address (str): The Cardano address string.

        Returns:
            str: The payment credential.
        """
        return Address.from_primitive(cardano_address).payment_part.payload.hex()
