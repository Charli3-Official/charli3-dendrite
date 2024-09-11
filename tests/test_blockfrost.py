"""Tests for the BlockFrost backend."""

import os
import pytest
from pycardano import Address

from charli3_dendrite.backend import get_backend, set_backend
from charli3_dendrite.backend.blockfrost import BlockFrostBackend
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dexs.amm.sundae import SundaeSwapCPPState


@pytest.fixture(scope="module")
def blockfrost_backend() -> BlockFrostBackend:
    """Fixture to set up and return a BlockFrost backend instance."""
    project_id = os.environ.get("PROJECT_ID")
    if not project_id:
        pytest.skip("BLOCKFROST_PROJECT_ID environment variable not set")
    backend = BlockFrostBackend(project_id)
    set_backend(backend)
    return backend


@pytest.fixture(autouse=True)
def use_blockfrost_backend(blockfrost_backend):
    """Ensure BlockFrost backend is used for all tests in this file."""
    set_backend(blockfrost_backend)
    assert isinstance(get_backend(), BlockFrostBackend)
    yield


def test_get_pool_utxos() -> None:
    """Test the get_pool_utxos method."""
    selector = SundaeSwapCPPState.pool_selector()
    selector_dict = selector.model_dump()

    existing_assets = selector_dict.pop("assets", [])
    if existing_assets is None:
        existing_assets = []
    elif not isinstance(existing_assets, list):
        existing_assets = [existing_assets]

    specific_asset = (
        "8e51398904a5d3fc129fbf4f1589701de23c7824d5c90fdb9490e15a434841524c4933"
    )
    assets = existing_assets + [specific_asset]
    result = get_backend().get_pool_utxos(
        limit=100, historical=False, assets=assets, **selector_dict
    )
    assert isinstance(result, PoolStateList)
    assert len(result) > 0
    assert all(hasattr(pool, "address") for pool in result)


def test_get_pool_in_tx() -> None:
    """Test the get_pool_in_tx method."""
    tx_hash = "14e59f304767ea9a659fe3dce74c1ea3837652b5008fab0bd6c56b023ad3f227"
    selector = SundaeSwapCPPState.pool_selector()
    result = get_backend().get_pool_in_tx(tx_hash, **selector.model_dump())
    assert isinstance(result, PoolStateList)
    assert len(result) > 0


def test_last_block() -> None:
    """Test the last_block method."""
    result = get_backend().last_block(last_n_blocks=2)
    assert len(result) == 2
    assert all(hasattr(block, "block_no") for block in result)


def test_get_pool_utxos_in_block() -> None:
    """Test the get_pool_utxos_in_block method."""
    result = get_backend().get_pool_utxos_in_block(10674256)
    assert isinstance(result, PoolStateList)
    assert len(result) > 0


def test_get_script_from_address() -> None:
    """Test the get_script_from_address method."""
    address = Address.from_primitive(
        "addr1w9qzpelu9hn45pefc0xr4ac4kdxeswq7pndul2vuj59u8tqaxdznu",
    )
    result = get_backend().get_script_from_address(address)
    assert result is not None
    assert hasattr(result, "script")


def test_get_datum_from_address() -> None:
    """Test the get_datum_from_address method."""
    address = Address.from_primitive(
        "addr1wyxq728k9pka686lzfkdyv60marz94swec9ef9mkxfqhyfqezqyjz",
    )
    result = get_backend().get_datum_from_address(address)
    assert result is not None
    assert hasattr(result, "datum_cbor")


def test_not_implemented_methods() -> None:
    """Test methods that are not implemented in BlockFrost backend."""
    with pytest.raises(NotImplementedError):
        get_backend().get_historical_order_utxos(["stake_address"])

    with pytest.raises(NotImplementedError):
        get_backend().get_order_utxos_by_block_or_tx(["stake_address"])

    with pytest.raises(NotImplementedError):
        get_backend().get_cancel_utxos(["stake_address"])
