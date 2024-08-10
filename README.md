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
- 🔗 On-chain Data Integration: Connect with Blockfrost or custom data providers
- 🛠 Extensible Architecture: Easily add support for new DEXs and features


## Quick Start

### Installation

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

## Upcoming DEXs

- CardanoSwaps
- Metadex
- CSwap

## Not Supported

- TeddySwap
- Cerra
- SaturnSwap
- Splash

Each DEX is implemented as a separate module within the `charli3 dendrite.dexs.amm` package.
## Core Components
### AbstractPoolState
The `AbstractPoolState` class in `amm_base.py` provides the base implementation for AMM (Automated Market Maker) pool states. It includes methods for:

- Extracting pool information
- Handling swap operations
- Calculating prices and TVL (Total Value Locked)

### AbstractPairState
The `AbstractPairState` class in `base.py` defines the interface for all pair states (both AMM and orderbook-based). It includes abstract methods that must be implemented by specific DEX classes.
### Assets
The `Assets` class in `models.py` represents a collection of assets (tokens) and their quantities. It provides utility methods for working with asset collections, including addition and subtraction operations.
## Configuration
Charli3 Dendrite can be configured using environment variables or a `.env` file. See `sample.env` for an example of the configuration options.

### Data Provider Configuration

Charli3 Dendrite supports multiple data providers to fetch on-chain data. Currently, it supports Blockfrost and DBSync, with plans to add Ogmios and Kupo in the future.

#### Blockfrost Configuration

Blockfrost is the default data provider for Charli3 Dendrite. To use Blockfrost, you need to set up your API key in the environment variables or `.env` file:
```
PROJECT_ID="your-blockfrost-project-id"
NETWORK="mainnet"  # or "testnet" for the Cardano testnet
```
#### DBSync Configuration
Charli3 Dendrite also supports using a DBSync instance as a data provider. To configure DBSync, set the following environment variables:
```bash
DBSYNC_HOST="your-dbsync-host"
DBSYNC_PORT="your-dbsync-port"
DBSYNC_DB_NAME="your-dbsync-database-name"
DBSYNC_USER="your-dbsync-username"
DBSYNC_PASS="your-dbsync-password"
```
#### Future Plans: Ogmios and Kupo
We are planning to add support for Ogmios and Kupo as additional data providers in future releases. These will offer alternative ways to interact with the Cardano blockchain and may provide performance improvements or additional features.
Stay tuned for updates on the integration of these providers. Once implemented, they will be configurable in a similar manner to the existing providers.
### Wallet Configuration
Charli3 Dendrite supports wallet integration for performing transactions. You can configure a wallet using a mnemonic phrase:
```bash
WALLET_MNEMONIC="your wallet mnemonic phrase"
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
For parallel test execution:
```bash
poetry run pytest -n auto
```
## Contributing
Contributions to Charli3 Dendrite are welcome! Please refer to the `CONTRIBUTING.md` file for guidelines on how to contribute to the project.
