"""Example script to test the backend functions."""

import json
import logging
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Dict

from cardex import MinswapV2CPPState
from cardex import SundaeSwapCPPState
from cardex.backend import get_backend
from cardex.backend import set_backend
from cardex.backend.dbsync import DbsyncBackend
from cardex.dexs.amm.amm_base import AbstractPoolState
from cardex.dexs.core.errors import InvalidLPError
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from pycardano import Address

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("cardex")

DEXS: list[type[AbstractPoolState]] = [
    SundaeSwapCPPState,
    MinswapV2CPPState,
    # Add other DEX states here
]


def save_to_file(data: Dict[str, Any], filename: str = "blockchain_data.json"):
    """Save the blockchain data to a local file."""
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Data successfully saved to %s", filename)
    except Exception as e:
        logger.error("Error saving data to file: %s", e)


def test_get_pool_utxos(
    backend: DbsyncBackend,
    dex: type[AbstractPoolState],
) -> Dict[str, Any]:
    """Test get_pool_utxos function."""
    logger.info("Testing get_pool_utxos for %s...", dex.__name__)
    selector = dex.pool_selector()
    result = backend.get_pool_utxos(
        limit=100000,
        historical=False,
        **selector.model_dump(),
    )

    pool_data = {}
    for pool in result:
        try:
            d = dex.model_validate(pool.model_dump())
            pool_data[d.pool_id] = {
                "assets": d.assets.model_dump(),
                "fee": d.fee,
                "last_update": d.block_time,
            }
        except (NoAssetsError, InvalidLPError, InvalidPoolError):
            pass
        except Exception as e:
            logger.debug("%s: %s", dex.__name__, e)

    logger.info("Found %d pools for %s", len(pool_data), dex.__name__)
    return pool_data


def test_get_pool_in_tx(backend: DbsyncBackend) -> list[Dict[str, Any]]:
    """Test get_pool_in_tx function."""
    logger.info("Testing get_pool_in_tx...")
    tx_hash = "14e59f304767ea9a659fe3dce74c1ea3837652b5008fab0bd6c56b023ad3f227"
    selector = SundaeSwapCPPState.pool_selector()
    result = backend.get_pool_in_tx(tx_hash, **selector.model_dump())
    logger.info("Found %d pools in transaction %s", len(result), tx_hash)
    return [pool.model_dump() for pool in result]


def test_last_block(backend: DbsyncBackend) -> list[Dict[str, Any]]:
    """Test last_block function."""
    logger.info("Testing last_block...")
    blocks = backend.last_block(last_n_blocks=2)
    block_data = [block.model_dump() for block in blocks]
    logger.info("Retrieved data for %d blocks", len(block_data))
    return block_data


def test_get_pool_utxos_in_block(backend: DbsyncBackend) -> list[Dict[str, Any]]:
    """Test get_pool_utxos_in_block function."""
    logger.info("Testing get_pool_utxos_in_block...")
    blocks = backend.last_block(last_n_blocks=1)
    if not blocks:
        return []
    result = backend.get_pool_utxos_in_block(blocks[0].block_no)
    logger.info("Found %d pool UTXOs in block %d", len(result), blocks[0].block_no)
    return [pool.model_dump() for pool in result[:10]]  # Return first 10 for brevity


def test_get_script_from_address(backend: DbsyncBackend) -> Dict[str, Any]:
    """Test get_script_from_address function."""
    logger.info("Testing get_script_from_address...")
    address = Address.from_primitive(
        "addr1w9qzpelu9hn45pefc0xr4ac4kdxeswq7pndul2vuj59u8tqaxdznu",
    )
    result = backend.get_script_from_address(address)
    logger.info("Retrieved script for address %s", address)
    return result.model_dump()


def test_get_datum_from_address(backend: DbsyncBackend) -> Dict[str, Any] | None:
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


def test_get_historical_order_utxos(backend: DbsyncBackend) -> list[Dict[str, Any]]:
    """Test get_historical_order_utxos function."""
    logger.info("Testing get_historical_order_utxos...")
    stake_addresses = [
        "addr1z8ax5k9mutg07p2ngscu3chsauktmstq92z9de938j8nqa7zcka2k2tsgmuedt4xl2j5awftvqzmmv3vs2yduzqxfcmsyun6n3",
    ]
    after_time = datetime.now() - timedelta(days=1)  # Look at last 24 hours
    result = backend.get_historical_order_utxos(
        stake_addresses,
        after_time=after_time,
        limit=10,
    )
    logger.info("Found %d historical order UTXOs", len(result))
    return [utxo.model_dump() for utxo in result]


def test_get_order_utxos_by_block_or_tx(backend: DbsyncBackend) -> list[Dict[str, Any]]:
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


def test_get_cancel_utxos(backend: DbsyncBackend) -> list[Dict[str, Any]]:
    """Test get_cancel_utxos function."""
    logger.info("Testing get_cancel_utxos...")
    stake_addresses = [
        "addr1z8ax5k9mutg07p2ngscu3chsauktmstq92z9de938j8nqa7zcka2k2tsgmuedt4xl2j5awftvqzmmv3vs2yduzqxfcmsyun6n3",
    ]
    after_time = datetime.now() - timedelta(days=1)  # Look at last 24 hours
    result = backend.get_cancel_utxos(stake_addresses, after_time=after_time, limit=10)
    logger.info("Found %d cancel UTXOs", len(result))
    return [utxo.model_dump() for utxo in result]


def main():
    """Main function to run all tests."""
    set_backend(DbsyncBackend())
    backend = get_backend()
    all_data = {}

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
    all_data["historical_order_utxos"] = test_get_historical_order_utxos(backend)
    all_data["order_utxos_by_block"] = test_get_order_utxos_by_block_or_tx(backend)
    all_data["cancel_utxos"] = test_get_cancel_utxos(backend)

    # Save all collected data to a file
    save_to_file(all_data)
    logger.info("All tests completed. Data saved to blockchain_data.json")


if __name__ == "__main__":
    main()
