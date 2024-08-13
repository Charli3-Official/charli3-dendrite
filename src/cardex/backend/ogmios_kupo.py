"""Backend that uses Ogmios for chain context and Kupo for pool state information"""

from datetime import datetime
import logging
from typing import Any
from typing import List
from typing import Optional
from typing import Union
from itertools import groupby

import requests
from pycardano import Address
from pycardano import Network
from ogmios import OgmiosChainContext

from cardex.backend.backend_base import AbstractBackend
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import BlockInfo
from cardex.dataclasses.models import BlockList
from cardex.dataclasses.models import PoolStateInfo
from cardex.dataclasses.models import PoolStateList
from cardex.dataclasses.models import ScriptReference
from cardex.dataclasses.models import SwapStatusInfo
from cardex.dataclasses.models import SwapSubmitInfo
from cardex.dataclasses.models import SwapTransactionInfo
from cardex.dataclasses.models import SwapExecuteInfo
from cardex.dataclasses.models import SwapTransactionList

SHELLEY_START = 1596491091
SLOT_LENGTH = 1
SHELLEY_SLOT_OFFSET = 4924800

logger = logging.getLogger(__name__)


class OgmiosKupoBackend(AbstractBackend):
    """Backend that uses Ogmios for chain context and Kupo for pool state information"""

    def __init__(
        self,
        ogmios_url: str,
        kupo_url: str,
        network: Network,
    ):
        _, ws_string = ogmios_url.split("ws://")
        self.ws_url, self.port = ws_string.split(":")
        self.ogmios_context = OgmiosChainContext(
            host=self.ws_url, port=int(self.port), network=network
        )
        self.kupo_url = kupo_url

    def _kupo_request(self, endpoint: str, params: Optional[dict] = None) -> Any:
        url = f"{self.kupo_url}/{endpoint}"
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def _pool_state_from_kupo(self, match_data: dict) -> PoolStateInfo:
        assets = self._format_assets(match_data["value"])

        current_slot = match_data["created_at"]["slot_no"]
        block_time = SHELLEY_START + current_slot - 4924800

        datum_hash = match_data.get("datum_hash")
        datum_cbor = self._get_datum_cbor(datum_hash) if datum_hash else ""

        return PoolStateInfo(
            address=match_data["address"],
            tx_hash=match_data["transaction_id"],
            tx_index=match_data["output_index"],
            block_time=block_time,
            block_index=match_data["transaction_index"],
            block_hash=match_data["created_at"]["header_hash"],
            datum_hash=datum_hash,
            datum_cbor=datum_cbor,
            assets=assets,
            plutus_v2=match_data.get("script_hash") is not None,
        )

    def get_pool_utxos(
        self,
        assets: Optional[List[str]] = None,
        addresses: Optional[List[str]] = None,
        limit: int = 1000,
        page: int = 0,
        historical: bool = True,
    ) -> PoolStateList:
        pool_states = []
        for address in addresses:
            params = {
                "limit": limit,
                "offset": page * limit,
            }
            if assets:
                params["policy_id"] = assets[0][:56]
                params["asset_name"] = assets[0][56:] if len(assets[0]) > 56 else None

            matches = self._kupo_request(f"matches/{address}?unspent", params=params)

            for match in matches:
                pool_state = self._pool_state_from_kupo(match)
                pool_states.append(pool_state)

        return PoolStateList(root=pool_states)

    def get_pool_in_tx(
        self,
        tx_hash: str,
        assets: Optional[List[str]] = None,
        addresses: Optional[List[str]] = None,
    ) -> PoolStateList:
        pool_states = []
        for address in addresses:
            params = {"transaction_id": tx_hash, "order": "most_recent_first"}
            if assets:
                params["policy_id"] = assets[0][:56]
                params["asset_name"] = assets[0][56:] if len(assets[0]) > 56 else None

            matches = self._kupo_request(f"matches/{address}", params=params)

            for match in matches:
                pool_state = self._pool_state_from_kupo(match)
                pool_states.append(pool_state)

        return PoolStateList(root=pool_states)

    def last_block(self, last_n_blocks: int = 2) -> BlockList:
        # Get health information from Ogmios
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

        # Process Kupo results
        block_data = {}
        for match in kupo_matches:
            header_hash = match["created_at"]["header_hash"]
            slot_no = match["created_at"]["slot_no"]
            tx_hash = match["transaction_id"]
            if header_hash not in block_data:
                block_data[header_hash] = {"slot_no": slot_no, "tx_hashes": set()}
            block_data[header_hash]["tx_hashes"].add(tx_hash)

        # Sort blocks by slot number (descending) and take the last_n_blocks
        sorted_blocks = sorted(
            block_data.items(), key=lambda x: x[1]["slot_no"], reverse=True
        )[:last_n_blocks]

        blocks = []
        for i, (header_hash, block_info) in enumerate(sorted_blocks):
            current_slot = block_info["slot_no"]
            current_block_no = latest_block_no - i

            # Calculate epoch slot
            slot_diff = latest_slot - current_slot
            epoch_slot = (
                current_epoch_slot - slot_diff
            ) % 432000  # Assuming 432000 slots per epoch

            # Convert slot to timestamp (assuming 1 second per slot and a known start time)
            shelley_start = 1596491091
            block_time = shelley_start + current_slot - 4924800

            blocks.append(
                BlockInfo(
                    epoch_slot_no=epoch_slot,
                    block_no=current_block_no,
                    tx_count=len(block_info["tx_hashes"]),
                    block_time=block_time,
                )
            )

        return BlockList(root=blocks)

    def _ogmios_request(self, endpoint: str) -> dict:
        url = f"http://{self.ws_url}:{self.port}/{endpoint}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()

    def get_pool_utxos_in_block(self, block_no: int, block_time: int) -> PoolStateList:
        """Get pool UTXOs created in a specific block
        Note: Kupo needs block time or slot number to query for UTXOs and not block number
        """

        block_slot = block_time - SHELLEY_START + 4924800
        params = {"created_after": block_slot, "order": "most_recent_first"}
        matches = self._kupo_request("matches", params=params)
        pool_states = [
            self._pool_state_from_kupo(match)
            for match in matches
            if match.get("datum_hash")
        ]
        return PoolStateList(root=pool_states)

    def get_script_from_address(self, address: Address) -> ScriptReference:
        script_hash = address.payment_part.payload.hex()
        script_data = self._kupo_request(f"scripts/{script_hash}")

        if script_data is None:
            return ScriptReference(
                tx_hash=None,
                tx_index=None,
                address=None,
                assets=None,
                datum_hash=None,
                datum_cbor=None,
                script=None,
            )

        return ScriptReference(
            tx_hash=None,
            tx_index=None,
            address=None,
            assets=None,
            datum_hash=None,
            datum_cbor=None,
            script=script_data["script"],
        )

    def _time_to_slot(self, time_value: Union[int, datetime]) -> int:
        """Convert a time value (Unix timestamp or datetime) to a slot number."""
        if isinstance(time_value, datetime):
            unix_time = int(time_value.timestamp())
        elif isinstance(time_value, int):
            unix_time = time_value

        return ((unix_time - SHELLEY_START) // SLOT_LENGTH) + SHELLEY_SLOT_OFFSET

    def _slot_to_time(self, slot: int) -> int:
        """Convert a slot number to a Unix timestamp."""
        return SHELLEY_START + (slot - SHELLEY_SLOT_OFFSET) * SLOT_LENGTH

    # def get_historical_order_utxos(
    #     self,
    #     stake_addresses: List[str],
    #     after_time: Optional[Union[datetime, int]] = None,
    #     limit: int = 1000,
    #     page: int = 0,
    # ) -> SwapTransactionList:
    #     all_swap_transactions = []

    #     payment_credentials = [
    #         Address.decode(addr).payment_part.payload.hex() for addr in stake_addresses
    #     ]

    #     for cred in payment_credentials:
    #         params = {
    #             "limit": limit,
    #             "offset": page * limit,
    #             "order": "most_recent_first",
    #         }

    #         if after_time is not None:
    #             after_slot = self._time_to_slot(after_time)
    #             params["created_after"] = after_slot

    #         all_utxos = self._kupo_request(f"matches/{cred}/*", params=params)
    #         logger.debug(
    #             f"Retrieved {len(all_utxos)} matches for payment credential: {cred}"
    #         )

    #         # Sort UTXOs by transaction ID to match dbsync query ordering
    #         all_utxos.sort(key=lambda utxo: utxo["transaction_id"])

    #         swap_transactions = []
    #         current_tx = None
    #         current_swap_statuses = []

    #         for utxo in all_utxos:
    #             if utxo.get("datum_hash"):
    #                 try:
    #                     parsed_match = self._parse_kupo_match(utxo, cred)
    #                     all_swap_transactions.append(parsed_match)
    #                 except Exception as e:
    #                     logger.error(f"Error processing match: {e}")
    #                     logger.error(f"Match data: {utxo}")
    #                     continue

    #     logger.info(
    #         f"Successfully processed {len(all_swap_transactions)} swap transactions"
    #     )
    #     return SwapTransactionList.model_validate(all_swap_transactions)

        #     for match in matches:
        #         if match.get("datum_hash"):
        #             try:
        #                 swap_status_data = SwapStatusInfo.from_kupo(
        #                     match, stake_address
        #                 )
        #                 swap_status = SwapStatusInfo.model_validate(swap_status_data)

        #                 # If the UTXO has been spent, add swap_output information
        #                 if match.get("spent_at"):
        #                     swap_output = SwapExecuteInfo(
        #                         address=match["address"],
        #                         tx_hash=match["transaction_id"],
        #                         tx_index=match["output_index"],
        #                         block_time=self._slot_to_time(
        #                             match["spent_at"]["slot_no"]
        #                         ),
        #                         block_index=match["transaction_index"],
        #                         block_hash=match["spent_at"]["header_hash"],
        #                         assets=SwapStatusInfo._format_assets(match["value"]),
        #                     )
        #                     swap_status.swap_output = swap_output

        #                 swap_transaction = SwapTransactionInfo(root=[swap_status])
        #                 all_swap_transactions.append(swap_transaction)
        #             except Exception as e:
        #                 logger.error(f"Error processing match: {e}")
        #                 logger.error(f"Match data: {match}")
        #                 continue

        # logger.info(
        #     f"Successfully processed {len(all_swap_transactions)} swap transactions"
        # )
        # return SwapTransactionList(root=all_swap_transactions)

    # def get_order_utxos_by_block_or_tx(
    #     self,
    #     stake_addresses: List[str],
    #     out_tx_hash: Optional[List[str]] = None,
    #     in_tx_hash: Optional[List[str]] = None,
    #     block_no: Optional[int] = None,
    #     after_block: Optional[int] = None,
    #     limit: int = 1000,
    #     page: int = 0,
    # ) -> SwapTransactionList:
    #     swap_transactions = []
    #     for address in stake_addresses:
    #         params = {
    #             "limit": limit,
    #             "offset": page * limit,
    #             "order": "most_recent_first",
    #         }
    #         if block_no:
    #             params["created_at"] = block_no
    #         elif after_block:
    #             params["created_after"] = after_block

    #         matches = self._kupo_request(f"matches/{address}", params=params)

    #         for match in matches:
    #             if match.get("datum_hash"):
    #                 swap_submit = SwapSubmitInfo(
    #                     address_inputs=[match["address"]],
    #                     address_stake=address,
    #                     assets=Assets(root=match["value"]),
    #                     block_hash=match["created_at"]["block_hash"],
    #                     block_time=match["created_at"]["slot_no"],
    #                     block_index=match["created_at"]["block_index"],
    #                     datum_hash=match["datum_hash"],
    #                     datum_cbor=match.get("datum", ""),
    #                     metadata=None,  # Kupo doesn't provide metadata directly
    #                     tx_hash=match["transaction_id"],
    #                     tx_index=match["transaction_index"],
    #                 )
    #                 swap_status = SwapStatusInfo(
    #                     swap_input=swap_submit, swap_output=None
    #                 )
    #                 swap_transactions.append(SwapTransactionInfo(root=[swap_status]))

    #     # Filter by out_tx_hash and in_tx_hash if provided
    #     if out_tx_hash:
    #         swap_transactions = [
    #             st
    #             for st in swap_transactions
    #             if st.root[0].swap_input.tx_hash in out_tx_hash
    #         ]
    #     if in_tx_hash:
    #         swap_transactions = [
    #             st
    #             for st in swap_transactions
    #             if st.root[0].swap_output
    #             and st.root[0].swap_output.tx_hash in in_tx_hash
    #         ]

    #     return SwapTransactionList(root=swap_transactions)

    # def get_cancel_utxos(
    #     self,
    #     stake_addresses: List[str],
    #     block_no: Optional[int] = None,
    #     after_time: Optional[Union[datetime, int]] = None,
    #     limit: int = 1000,
    #     page: int = 0,
    # ) -> SwapTransactionList:
    #     swap_transactions = []
    #     for address in stake_addresses:
    #         params = {
    #             "limit": limit,
    #             "offset": page * limit,
    #             "order": "most_recent_first",
    #         }
    #         if block_no:
    #             params["created_at"] = block_no
    #         if after_time:
    #             params["created_after"] = (
    #                 after_time.isoformat()
    #                 if isinstance(after_time, datetime)
    #                 else after_time
    #             )

    #         matches = self._kupo_request(f"matches/{address}", params=params)

    #         for match in matches:
    #             if match.get("datum_hash"):
    #                 swap_submit = SwapSubmitInfo(
    #                     address_inputs=[match["address"]],
    #                     address_stake=address,
    #                     assets=Assets(root=match["value"]),
    #                     block_hash=match["created_at"]["block_hash"],
    #                     block_time=match["created_at"]["slot_no"],
    #                     block_index=match["created_at"]["block_index"],
    #                     datum_hash=match["datum_hash"],
    #                     datum_cbor=match.get("datum", ""),
    #                     metadata=None,  # Kupo doesn't provide metadata directly
    #                     tx_hash=match["transaction_id"],
    #                     tx_index=match["transaction_index"],
    #                 )
    #                 swap_status = SwapStatusInfo(
    #                     swap_input=swap_submit, swap_output=None
    #                 )
    #                 swap_transactions.append(SwapTransactionInfo(root=[swap_status]))

    #     return SwapTransactionList(root=swap_transactions)

    def get_datum_from_address(
        self, address: str, asset: Optional[str] = None
    ) -> Optional[ScriptReference]:
        params = {"unspent": None, "order": "most_recent_first"}
        if asset:
            params["policy_id"] = asset[:56]
            params["asset_name"] = asset[56:] if len(asset) > 56 else None

        matches = self._kupo_request(f"matches/{address}", params=params)
        if matches:
            match = matches[0]
            datum_hash = match.get("datum_hash")
            if datum_hash:
                datum = self._kupo_request(f"datums/{datum_hash}")
                assets = self._format_assets(match["value"])
                return ScriptReference(
                    tx_hash=match["transaction_id"],
                    tx_index=match["output_index"],
                    address=address.encode(),
                    assets=assets,
                    datum_hash=datum_hash,
                    datum_cbor=datum["datum"],
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
        policy = assets.unit()[:56]
        name = assets.unit()[56:]
        asset_id = f"{policy}.{name}"

        # Prepare parameters for Kupo request
        params = {
            "policy_id": policy,
            "asset_name": name if name else None,
            "order": "most_recent_first",
        }

        if block_time:
            params["created_before"] = self._time_to_slot(block_time)

        try:
            # Query Kupo for matches
            matches = self._kupo_request(
                "matches/55ff0e63efa0694e8065122c552e80c7b51768b7f20917af25752a7c/*",
                params=params,
            )

            # Filter and process matches
            for match in matches:
                # Query all outputs for the transaction
                tx_outputs = self._kupo_request(f"matches/*@{match['transaction_id']}")

                # Find the first output that contains the asset and is not an AXO address
                for output in tx_outputs:
                    output_payment_cred = output["address"][
                        2:58
                    ]  # Extract payment credential
                    if (
                        asset_id in output["value"].get("assets", {})
                        and output_payment_cred
                        != "55ff0e63efa0694e8065122c552e80c7b51768b7f20917af25752a7c"
                    ):
                        return output["address"]

            # If no suitable address found
            return None

        except Exception as e:
            logger.error(f"Error in get_axo_target: {e}")
            return None

    def _format_assets(self, value: dict) -> Assets:
        formatted_assets = {"lovelace": value["coins"]}
        for asset_id, amount in value.get("assets", {}).items():
            policy_id, asset_name = (
                asset_id.split(".", 1) if "." in asset_id else (asset_id, "")
            )
            formatted_asset_id = f"{policy_id}{asset_name}"
            formatted_assets[formatted_asset_id] = amount
        return Assets(root=formatted_assets)

    def _get_datum_cbor(self, datum_hash: Optional[str]) -> Optional[str]:
        if datum_hash:
            try:
                datum_data = self._kupo_request(f"datums/{datum_hash}")
                return datum_data["datum"]
            except Exception as e:
                logger.error(f"Error fetching datum CBOR: {e}")
        return None

    def _create_swap_submit_info(
        self, match: dict, stake_address: str
    ) -> SwapSubmitInfo:
        return SwapSubmitInfo(
            address_inputs=[match["address"]],
            address_stake=stake_address,
            assets=self._format_assets(match["value"]),
            block_hash=match["created_at"]["header_hash"],
            block_time=self._slot_to_time(match["created_at"]["slot_no"]),
            block_index=match["transaction_index"],
            datum_hash=match["datum_hash"],
            datum_cbor=self._get_datum_cbor(match["datum_hash"]),
            metadata=None,
            tx_hash=match["transaction_id"],
            tx_index=match["output_index"],
        )

    def _create_swap_execute_info(self, match: dict) -> Optional[SwapExecuteInfo]:
        if match.get("spent_at"):
            return SwapExecuteInfo(
                address=match["address"],
                assets=self._format_assets(match["value"]),
                block_hash=match["spent_at"]["header_hash"],
                block_time=self._slot_to_time(match["spent_at"]["slot_no"]),
                block_index=match["transaction_index"],
                tx_hash=match["transaction_id"],
                tx_index=match["output_index"],
            )
        return None

    def _parse_kupo_match(self, match: dict, payment_credential: str) -> dict:
        """
        Parse a single Kupo match into the format expected by SwapTransactionList.
        """
        # Extract basic information
        submit_data = {
            "submit_address_inputs": [match["address"]],
            "submit_address_stake": f"stake1{payment_credential}",  # Reconstruct stake address
            "submit_tx_hash": match["transaction_id"],
            "submit_tx_index": match["output_index"],
            "submit_block_hash": match["created_at"]["header_hash"],
            "submit_block_time": self._slot_to_time(match["created_at"]["slot_no"]),
            "submit_block_index": match["transaction_index"],
            "submit_metadata": None,  # Kupo doesn't provide metadata
            "submit_assets": self._format_assets(match["value"]),
            "submit_datum_hash": match["datum_hash"],
            "submit_datum_cbor": "some_cbor",  # Kupo doesn't provide datum CBOR
        }

        # If the UTXO has been spent, add execution data
        if match.get("spent_at"):
            execute_data = {
                "address": match["address"],
                "tx_hash": match[
                    "transaction_id"
                ],  # Use the same transaction_id as submit
                "tx_index": match[
                    "output_index"
                ],  # Use the same output_index as submit
                "block_time": self._slot_to_time(match["spent_at"]["slot_no"]),
                "block_index": match[
                    "transaction_index"
                ],  # Use the same transaction_index as submit
                "block_hash": match["spent_at"]["header_hash"],
                "datum_hash": match["datum_hash"],
                "datum_cbor": None,
                "assets": self._format_assets(match["value"]),
                "plutus_v2": match.get("script_hash") is not None,
            }
            submit_data.update(execute_data)

        return submit_data
