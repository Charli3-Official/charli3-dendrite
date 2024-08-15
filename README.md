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
- 🔗 On-chain Data Integration: Connect with DB-sync
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

Each DEX is implemented as a separate module within the `charli3 dendrite.dexs.amm` package.


## Configuration
Charli3 Dendrite can be configured using environment variables or a `.env` file. See `sample.env` for an example of the configuration options.

#### Blockfrost Configuration

Blockfrost is required for the test suite. To use Blockfrost, you must configure your API key by setting it in your environment variables or in a `.env` file:

```
PROJECT_ID="your-blockfrost-project-id"
NETWORK="mainnet"  # or "testnet" for the Cardano testnet
```

#### DBSync Configuration
Charli3 Dendrite supports using a DBSync instance as a blockchain connection. To configure DBSync, set the following environment variables:

```bash
DBSYNC_HOST="your-dbsync-host"
DBSYNC_PORT="your-dbsync-port"
DBSYNC_DB_NAME="your-dbsync-database-name"
DBSYNC_USER="your-dbsync-username"
DBSYNC_PASS="your-dbsync-password"
```
#### Future Plans: Ogmios and Kupo
We plan to extend support for additional data providers, specifically Ogmios/Kupo and Blockfrost, in future releases. These providers will offer alternative methods for interacting with the Cardano blockchain and may deliver performance enhancements or additional features.

Please stay informed about upcoming updates regarding the integration of these providers. Once implemented, they will be configurable in a manner consistent with the existing providers.

## Use Cases 
### Retrieving Orders and Pool Data on VyFi

To retrieve all orders from the VyFi DEX, the global backend must first be configured. This configuration is achieved by invoking the `set_backend` function, which sets up the `AbstractBackend` class. The `AbstractBackend` interface enables seamless interaction with various Cardano blockchain connections, including db-sync, and Ogmios/Kupo. If no backend is explicitly specified, the function defaults to configurations based on environment variables to select the appropriate backend.
```
set_backend(backend)                   # Set the current backend according to environment variables.
backend: DbsyncBackend = get_backend() # Retrieve the current backend instance.
```
The `DbsyncBackend` class offers specialized queries for interacting with the underlying PostgreSQL database, which stores blockchain blocks and transactions. To retrieve all orders, one can utilize the `pool_selector` method to request pool information from the VyFi API. After acquiring the relevant pool data and configuring the backend, the `backend.get_pool_utxos()` method can be employed to query the latest UTxOs associated with the selected pool.
```python
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
```
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
