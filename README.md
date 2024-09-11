<div align="center">
    <h1 align="center">Charli3 Dendrite</h1>
    <p align="center">Python SDK for interacting with Cardano DEXs</p>
    <p>
        <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python version">
        <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
        <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs welcome">
    </p>
</div>

## Overview

Charli3 Dendrite is a powerful Python SDK designed for seamless interaction with multiple Decentralized Exchanges (DEXs) on the Cardano blockchain. It provides a unified interface for developers to access various DEX functionalities, simplifying the process of building applications in the Cardano ecosystem.

## Key Features

- 🔄 Multi-DEX Support: Integrate with Minswap, MuesliSwap, Spectrum, SundaeSwap, VyFi, GeniusYield, Axo, and WingRiders
- 💧 Liquidity Pool Data: Fetch and analyze pool information across different DEXs
- 💱 Swap Operations: Execute token swaps with ease
- 🧩 Flexible Asset Handling: Manage various asset types and pool states efficiently
- 🔗 On-chain Data Integration: Connect with DB-sync, BlockFrost, and Ogmios/Kupo
- 🛠 Extensible Architecture: Easily add support for new DEXs and features


## Installation

```bash
# Using pip
pip install charli3_dendrite

# Using Poetry
poetry add charli3_dendrite
```

## Supported DEXs
Charli3 Dendrite currently supports the following Cardano DEXs:

- Minswap
- MuesliSwap
- Spectrum
- SundaeSwap
- VyFi
- WingRiders
- GeniusYield
- Axo

### Not Yet Implemented

- CardanoSwaps
- Metadex
- CSwap
- TeddySwap
- Cerra
- SaturnSwap
- Splash

Each DEX is implemented as a separate module within the `charli3_dendrite.dexs.amm` package.

## Configuration
Charli3 Dendrite can be configured using environment variables or a `.env` file. See `sample.env` for an example of the configuration options.

### Backend Configuration

Charli3 Dendrite supports multiple backend options for interacting with the Cardano blockchain:

#### DBSync Configuration

To use a DBSync instance as the blockchain connection, set the following environment variables:

```bash
DBSYNC_HOST="your-dbsync-host"
DBSYNC_PORT="your-dbsync-port"
DBSYNC_DB_NAME="your-dbsync-database-name"
DBSYNC_USER="your-dbsync-username"
DBSYNC_PASS="your-dbsync-password"
```

#### BlockFrost Configuration

To use BlockFrost as the backend, set the following environment variables:

```bash
BLOCKFROST_PROJECT_ID="your-blockfrost-project-id"
CARDANO_NETWORK="mainnet"  # or "testnet" for the Cardano testnet
```

#### Ogmios/Kupo Configuration

To use Ogmios and Kupo as the backend, set the following environment variables:

```bash
OGMIOS_URL="ws://your-ogmios-url:port"
KUPO_URL="http://your-kupo-url:port"
CARDANO_NETWORK="mainnet"  # or "testnet" for the Cardano testnet
```

The backend will be automatically selected based on the available environment variables. If multiple backend configurations are present, the priority order is: DBSync, BlockFrost, Ogmios/Kupo.

### Backend Limitations

While Charli3 Dendrite supports multiple backends, it's important to note that the BlockFrost and Ogmios/Kupo backends have some limitations compared to the DBSync backend:

- **BlockFrost Backend**: Due to limitations in the BlockFrost API, the following methods are not implemented:
  - `get_historical_order_utxos`
  - `get_order_utxos_by_block_or_tx`
  - `get_cancel_utxos`
  - `get_axo_target`

- **Ogmios/Kupo Backend**: The Ogmios/Kupo backend also has limitations due to the nature of these services:
  - `get_historical_order_utxos`
  - `get_order_utxos_by_block_or_tx`
  - `get_cancel_utxos`

These methods will raise a `NotImplementedError` when called using the BlockFrost or Ogmios/Kupo backends. If your application requires these functionalities, consider using the DBSync backend.

## Usage

### Retrieving Orders and Pool Data

To retrieve orders and pool data, first configure the global backend:

```python
from charli3_dendrite.backend import set_backend, get_backend
from charli3_dendrite.backend.dbsync import DbsyncBackend
from charli3_dendrite.backend.blockfrost import BlockFrostBackend
from charli3_dendrite.backend.ogmios_kupo import OgmiosKupoBackend
from pycardano import Network

# Choose one of the following backends:
# set_backend(DbsyncBackend())
# set_backend(BlockFrostBackend("your-project-id"))
set_backend(OgmiosKupoBackend("ws://ogmios-url:port", "http://kupo-url:port", Network.MAINNET))

backend = get_backend()
```

The `AbstractBackend` interface offers methods for interacting with the Cardano blockchain, regardless of the underlying data source. This abstraction allows seamless switching between different backends without changing your application code.

To retrieve pool information, use the `pool_selector` method provided by each DEX's state class:

```python
from charli3_dendrite import VyFiCPPState

selector = VyFiCPPState.pool_selector()
result = backend.get_pool_utxos(
    limit=100000,
    historical=False,
    **selector.model_dump(),
)
```
To process and parse the retrieved results (`list[PoolStateInfo]`), the following approach can be utilized:
```python
pool_data = {}
total_tvl = 0
for pool in result:
    d = dex.model_validate(pool.model_dump())
    try:
        logger.info("Get TVL %s", d.tvl)
        logger.info("Price %s", d.price)
        logger.info("Token name of asset A: %s", d.unit_a)
        logger.info("Token name of asset B: %s", d.unit_b)
    except NoAssetsError:
        pass
    except InvalidLPError:
        pass
    except InvalidPoolError:
        pass
    except Exception as e:
        logger.debug(f"{dex.__name__}: {e}")
```
This approach is applicable across all supported DEXs. For example, the following list of AbstractPoolState subclasses can be defined to support various DEX states:
```python
DEXS: list[AbstractPoolState] = [
    GeniusYieldOrderState,
    MinswapCPPState,
    MinswapV2CPPState,
    MinswapDJEDiUSDStableState,
    MinswapDJEDUSDCStableState,
    MinswapDJEDUSDMStableState,
    MuesliSwapCPPState,
    SpectrumCPPState,
    SundaeSwapCPPState,
    SundaeSwapV3CPPState,
    VyFiCPPState,
    WingRidersCPPState,
    WingRidersSSPState,
]
```
## Development
To set up the development environment:

1. Clone the repository
2. Install dependencies: `poetry install`
3. Set up pre-commit hooks: `pre-commit install`

## Running Tests
```bash
poetry run pytest --benchmark-disable -v --slow -n auto
```

## Contributing
Contributions to Charli3 Dendrite are welcome! Please refer to the `CONTRIBUTING.md` file for guidelines on how to contribute to the project.
