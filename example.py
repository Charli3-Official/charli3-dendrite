"""Example script to test the backend functions."""

import json
import logging
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Optional

from charli3_dendrite import MinswapCPPState  # type: ignore
from charli3_dendrite import MinswapV2CPPState  # type: ignore
from charli3_dendrite import MuesliSwapCPPState  # type: ignore
from charli3_dendrite import SpectrumCPPState  # type: ignore
from charli3_dendrite import SundaeSwapCPPState  # type: ignore
from charli3_dendrite import SundaeSwapV3CPPState  # type: ignore
from charli3_dendrite import VyFiCPPState  # type: ignore
from charli3_dendrite import WingRidersCPPState  # type: ignore
from charli3_dendrite.backend import AbstractBackend  # type: ignore
from charli3_dendrite.backend import DbsyncBackend  # type: ignore
from charli3_dendrite.backend import get_backend
from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.models import Assets  # type: ignore
from charli3_dendrite.dexs.amm.amm_base import AbstractPoolState  # type: ignore
from charli3_dendrite.dexs.core.errors import InvalidLPError  # type: ignore
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.dexs.core.errors import NoAssetsError
from pycardano import Address  # type: ignore

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("charli3_dendrite")

DEXS: list[type[AbstractPoolState]] = [
    SundaeSwapCPPState,
    MinswapV2CPPState,
    MinswapCPPState,
    WingRidersCPPState,
    VyFiCPPState,
    SpectrumCPPState,
    MuesliSwapCPPState,
    SundaeSwapV3CPPState,
    # Add other DEX states here
]


def save_to_file(data: dict[str, Any], filename: str = "blockchain_data.json") -> None:
    """Save the blockchain data to a local file."""
    try:
        with Path(filename).open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Data successfully saved to %s", filename)
    except OSError as e:
        logger.error("Error saving data to file: %s", e)


def test_get_pool_utxos(  # noqa: PLR0912
    backend: AbstractBackend,
    dex: type[AbstractPoolState],
) -> dict[str, Any]:
    """Test get_pool_utxos function for various DEX implementations."""
    logger.info("Testing get_pool_utxos for %s...", dex.__name__)

    specific_asset = (
        "8e51398904a5d3fc129fbf4f1589701de23c7824d5c90fdb9490e15a434841524c4933"
    )

    # Check if the DEX supports asset-based pool selection
    if (
        hasattr(dex, "pool_selector")
        and "assets" in dex.pool_selector.__code__.co_varnames
    ):
        try:
            selector = dex.pool_selector(assets=[specific_asset])
            logger.info("Using asset-based pool selection for %s", dex.__name__)
        except (AttributeError, TypeError, ValueError) as e:
            logger.error("Error in asset-based pool_selector: %s", str(e))
            return {}
    else:
        # Fallback to standard pool selection
        selector = dex.pool_selector()
        logger.info("Using standard pool selection for %s", dex.__name__)

    selector_dict = selector.model_dump()

    # Handle assets for get_pool_utxos
    assets = selector_dict.pop("assets", [])
    if assets is None:
        assets = []
    elif not isinstance(assets, list):
        assets = [assets]

    # Add the specific asset if it's not already included
    if specific_asset not in assets:
        assets.append(specific_asset)

    try:
        result = backend.get_pool_utxos(
            limit=10000,
            historical=False,
            assets=assets,
            **selector_dict,
        )
    except (ConnectionError, TimeoutError, ValueError) as e:
        logger.error("Error in get_pool_utxos: %s", str(e))
        return {}

    pool_data = {}
    for pool in result:
        try:
            d = dex.model_validate(pool.model_dump())
            pool_data[d.pool_id] = {
                "assets": d.assets.model_dump(),
                "fee": d.fee,
                "last_update": d.block_time,
            }
        except (NoAssetsError, InvalidLPError, InvalidPoolError) as e:
            logger.warning("Invalid pool data found: %s", e)
        except (TypeError, ValueError, KeyError) as e:
            logger.error("Unexpected error processing pool data: %s", str(e))

    logger.info("Found %d pools for %s", len(pool_data), dex.__name__)
    return pool_data


def test_get_pool_in_tx(backend: AbstractBackend) -> list[dict[str, Any]]:
    """Test get_pool_in_tx function."""
    logger.info("Testing get_pool_in_tx...")
    tx_hash = "14e59f304767ea9a659fe3dce74c1ea3837652b5008fab0bd6c56b023ad3f227"
    selector = SundaeSwapCPPState.pool_selector()
    result = backend.get_pool_in_tx(tx_hash, **selector.model_dump())
    logger.info("Found %d pools in transaction %s", len(result), tx_hash)
    return [pool.model_dump() for pool in result]


def test_last_block(backend: AbstractBackend) -> list[dict[str, Any]]:
    """Test last_block function."""
    logger.info("Testing last_block...")
    blocks = backend.last_block(last_n_blocks=2)
    block_data = [block.model_dump() for block in blocks]
    logger.info("Retrieved data for %d blocks", len(block_data))
    return block_data


def test_get_pool_utxos_in_block(backend: AbstractBackend) -> list[dict[str, Any]]:
    """Test get_pool_utxos_in_block function."""
    logger.info("Testing get_pool_utxos_in_block...")
    blocks = backend.last_block(last_n_blocks=1)
    if not blocks:
        return []
    result = backend.get_pool_utxos_in_block(blocks[0].block_no)
    logger.info("Found %d pool UTXOs in block %d", len(result), blocks[0].block_no)
    return [pool.model_dump() for pool in result[:10]]  # Return first 10 for brevity


def test_get_script_from_address(backend: AbstractBackend) -> dict[str, Any]:
    """Test get_script_from_address function."""
    logger.info("Testing get_script_from_address...")
    address = Address.from_primitive(
        "addr1w9qzpelu9hn45pefc0xr4ac4kdxeswq7pndul2vuj59u8tqaxdznu",
    )
    result = backend.get_script_from_address(address)
    logger.info("Retrieved script for address %s", address)
    return result.model_dump()


def test_get_datum_from_address(backend: AbstractBackend) -> str | None:
    """Test get_datum_from_address function."""
    logger.info("Testing get_datum_from_address...")
    address = Address.from_primitive(
        "addr1wyxq728k9pka686lzfkdyv60marz94swec9ef9mkxfqhyfqezqyjz",
    )
    result = backend.get_datum_from_address(address)
    if result:
        logger.info("Retrieved datum for address %s", address)
        return result.model_dump()
    logger.info("No datum found for address %s", address)
    return None


def test_get_historical_order_utxos(backend: AbstractBackend) -> list[dict[str, Any]]:
    """Test get_historical_order_utxos function."""
    logger.info("Testing get_historical_order_utxos...")
    stake_addresses = [
        "addr1z8ax5k9mutg07p2ngscu3chsauktmstq92z9de938j8nqa7zcka2k2tsgmuedt4xl2j5awftvqzmmv3vs2yduzqxfcmsyun6n3",
    ]
    after_time = datetime.now() - timedelta(hours=24)  # Look at last 24 hours
    after_time_unix = int(after_time.timestamp())  # Convert to Unix time
    result = backend.get_historical_order_utxos(
        stake_addresses,
        after_time=after_time_unix,
        limit=1000,
    )
    return [utxo.model_dump() for utxo in result]


def test_get_order_utxos_by_block_or_tx(
    backend: AbstractBackend,
) -> list[dict[str, Any]]:
    """Test get_order_utxos_by_block_or_tx function."""
    logger.info("Testing get_order_utxos_by_block_or_tx...")
    stake_addresses = [
        "addr1z8ax5k9mutg07p2ngscu3chsauktmstq92z9de938j8nqa7zcka2k2tsgmuedt4xl2j5awftvqzmmv3vs2yduzqxfcmsyun6n3",
    ]
    blocks = backend.last_block(last_n_blocks=1)
    if not blocks:
        return []
    result = backend.get_order_utxos_by_block_or_tx(
        stake_addresses,
        block_no=blocks[0].block_no,
        limit=10,
    )
    logger.info("Found %d order UTXOs in block %d", len(result), blocks[0].block_no)
    return [utxo.model_dump() for utxo in result]


def test_get_cancel_utxos(backend: AbstractBackend) -> list[dict[str, Any]]:
    """Test get_cancel_utxos function."""
    logger.info("Testing get_cancel_utxos...")
    stake_addresses = [
        "addr1z8ax5k9mutg07p2ngscu3chsauktmstq92z9de938j8nqa7zcka2k2tsgmuedt4xl2j5awftvqzmmv3vs2yduzqxfcmsyun6n3",
    ]
    after_time = datetime.now() - timedelta(hours=1)  # Look at last hour
    result = backend.get_cancel_utxos(
        stake_addresses,
        after_time=after_time,
        limit=1000,
    )
    logger.info("Found %d cancel UTXOs", len(result))
    return [utxo.model_dump() for utxo in result]


def test_get_axo_target(backend: AbstractBackend) -> Optional[str]:
    """Test get_axo_target function for both backends."""
    logger.info("Testing get_axo_target...")
    assets = Assets(
        root={
            "f13ac4d66b3ee19a6aa0f2a22298737bd907cc95121662fc971b5275535452494b45": 1,
        },
    )
    result = backend.get_axo_target(assets)
    logger.info("found at %s", result)
    return result


def main() -> None:
    """Main function to run all tests."""
    # Choose one of the following backends:

    # 1. DbsyncBackend (full functionality)
    set_backend(DbsyncBackend())

    # 2. OgmiosKupoBackend (some methods may not be implemented)
    # ruff: noqa: ERA001
    # set_backend(
    #     OgmiosKupoBackend(
    #         ogmios_url="ws://ogmios-url:1337",
    #         kupo_url="http://kupo-url:1442",
    #         network=Network.MAINNET,
    #     ),
    # )

    # 3. BlockFrostBackend (some methods may not be implemented)
    # ruff: noqa: ERA001
    # set_backend(BlockFrostBackend("blockfrost-api-key"))

    # Note: BlockFrost and Ogmios-Kupo backends may raise NotImplementedError
    # for methods like get_historical_order_utxos, get_order_utxos_by_block_or_tx,
    # and get_cancel_utxos due to limitations in their respective APIs.

    backend = get_backend()
    all_data: dict[str, Any] = {}

    # Test get_pool_utxos for each DEX
    for dex in DEXS:
        all_data[f"pool_utxos_{dex.__name__}"] = test_get_pool_utxos(backend, dex)

    # Test other functions
    all_data["pool_in_tx"] = test_get_pool_in_tx(backend)
    all_data["last_blocks"] = test_last_block(backend)
    all_data["pool_utxos_in_block"] = test_get_pool_utxos_in_block(backend)
    all_data["script_from_address"] = test_get_script_from_address(backend)
    datum_result = test_get_datum_from_address(backend)
    if datum_result is not None:
        all_data["datum_from_address"] = datum_result

    # The following methods may raise NotImplementedError
    # for BlockFrost and Ogmios-Kupo backends
    try:
        all_data["axo_target"] = test_get_axo_target(backend)
        all_data["historical_order_utxos"] = test_get_historical_order_utxos(backend)
        all_data["order_utxos_by_block"] = test_get_order_utxos_by_block_or_tx(backend)
        all_data["cancel_utxos"] = test_get_cancel_utxos(backend)
    except NotImplementedError as e:
        logger.warning(
            "Some methods are not implemented for the current backend: %s",
            e,
        )

    # Save all collected data to a file
    save_to_file(all_data)
    logger.info("All tests completed. Data saved to blockchain_data.json")


if __name__ == "__main__":
    main()
