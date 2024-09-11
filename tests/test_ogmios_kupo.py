"""Tests for the Ogmios-Kupo backend."""

import os
import pytest
from pycardano import Address, Network

from charli3_dendrite.backend import get_backend, set_backend
from charli3_dendrite.backend.ogmios_kupo import OgmiosKupoBackend
from charli3_dendrite.dataclasses.models import PoolStateList, Assets
from charli3_dendrite.dexs.amm.sundae import SundaeSwapCPPState


@pytest.fixture(scope="module")
def ogmios_kupo_backend() -> OgmiosKupoBackend:
    """Fixture to set up and return an Ogmios-Kupo backend instance."""
    ogmios_url = os.environ.get("OGMIOS_URL")
    kupo_url = os.environ.get("KUPO_URL")
    if not ogmios_url or not kupo_url:
        pytest.skip("OGMIOS_URL or KUPO_URL environment variable not set")
    backend = OgmiosKupoBackend(
        ogmios_url=ogmios_url,
        kupo_url=kupo_url,
        network=Network.MAINNET,
    )
    set_backend(backend)
    return backend


@pytest.fixture(autouse=True)
def use_ogmios_kupo_backend(ogmios_kupo_backend):
    """Ensure Ogmios-Kupo backend is used for all tests in this file."""
    set_backend(ogmios_kupo_backend)
    assert isinstance(get_backend(), OgmiosKupoBackend)
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
    # First, get a recent pool UTxO
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
    recent_pools = get_backend().get_pool_utxos(
        limit=100, historical=False, assets=assets, **selector_dict
    )

    assert len(recent_pools) > 0, "No recent pool UTxOs found"

    recent_tx_hash = recent_pools[0].tx_hash
    assert recent_tx_hash, "No transaction hash found for recent pool"

    # Now test get_pool_in_tx with this recent transaction
    result = get_backend().get_pool_in_tx(recent_tx_hash, **selector.model_dump())

    assert isinstance(result, PoolStateList)
    assert len(result) > 0, f"No pool found in transaction {recent_tx_hash}"

    # Additional checks to ensure the returned data is as expected
    for pool in result:
        assert (
            pool.tx_hash == recent_tx_hash
        ), f"Returned pool tx_hash {pool.tx_hash} doesn't match queried tx_hash {recent_tx_hash}"
        assert hasattr(pool, "address"), "Pool object doesn't have 'address' attribute"
        assert hasattr(pool, "assets"), "Pool object doesn't have 'assets' attribute"
        assert pool.assets is not None, "Pool assets are None"

    print(f"Successfully tested get_pool_in_tx with transaction: {recent_tx_hash}")


def test_last_block() -> None:
    """Test the last_block method."""
    result = get_backend().last_block(last_n_blocks=1)
    assert len(result) == 1
    assert all(hasattr(block, "block_no") for block in result)


def test_get_pool_utxos_in_block() -> None:
    """Test the get_pool_utxos_in_block method."""
    # Note: Ogmios-Kupo might require block_time as well
    last_block = get_backend().last_block(last_n_blocks=1)[0]
    result = get_backend().get_pool_utxos_in_block(
        last_block.block_no, last_block.block_time
    )
    assert isinstance(result, PoolStateList)
    assert len(result) >= 0


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


def test_get_axo_target() -> None:
    """Test the get_axo_target method."""
    assets = Assets(
        root={"420000029ad9527271b1b1e3c27ee065c18df70a4a4cfc3093a41a4441584f": 1}
    )
    result = get_backend().get_axo_target(assets)
    assert isinstance(result, str) or result is None


def test_not_implemented_methods() -> None:
    """Test methods that are not implemented in Ogmios-Kupo backend."""
    with pytest.raises(NotImplementedError):
        get_backend().get_historical_order_utxos(["stake_address"])

    with pytest.raises(NotImplementedError):
        get_backend().get_order_utxos_by_block_or_tx(["stake_address"])

    with pytest.raises(NotImplementedError):
        get_backend().get_cancel_utxos(["stake_address"])
