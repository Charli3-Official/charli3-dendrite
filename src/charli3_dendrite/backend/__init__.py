"""Backend Management Module for Charli3 Dendrite.

This module provides a centralized system for managing the backend used throughout
the Charli3 Dendrite application. It includes functions to get and set the global
backend, as well as to set a default backend based on environment variables.

The module uses a global variable to store the current backend instance, which
can be accessed and modified using the provided functions. This approach allows
for easy switching between different backend implementations and provides a
convenient way to set up a default backend.

Typical usage:
    from charli3_dendrite.backends import get_backend, set_backend
    from charli3_dendrite.backend.custom_backend import CustomBackend

    # Get the current backend (initializes a default if not set)
    backend = get_backend()

    # Set a custom backend
    set_backend(CustomBackend())

Note: This module relies on environment variables for setting up the default backend.
Ensure that the necessary environment variables are set before using this module.
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv  # type: ignore
from pycardano import Network  # type: ignore

from charli3_dendrite.backend.backend_base import AbstractBackend
from charli3_dendrite.backend.blockfrost import BlockFrostBackend
from charli3_dendrite.backend.dbsync import DbsyncBackend
from charli3_dendrite.backend.ogmios_kupo import OgmiosKupoBackend

# Load environment variables from .env file
load_dotenv()

# Global variable to store the current backend instance
BACKEND: Optional[AbstractBackend] = None


def get_backend() -> AbstractBackend:
    """Retrieve the current backend instance.

    If no backend has been set, this function attempts to set a default backend
    based on environment variables. If no default can be set, it raises a ValueError.

    Returns:
        AbstractBackend: The current backend instance.

    Raises:
        ValueError: If no backend has been set and no default can be determined.
    """
    if BACKEND is None:
        set_default_backend()
    if BACKEND is None:
        raise ValueError("Backend has not been set. Call set_backend() first.")
    return BACKEND


def set_backend(backend: AbstractBackend) -> None:
    """Set the global backend instance.

    Args:
        backend (AbstractBackend): The backend instance to set as the global backend.
    """
    global BACKEND  # noqa: PLW0603
    BACKEND = backend


def set_default_backend() -> None:
    """Attempt to set a default backend based on environment variables.

    This function checks for the presence of specific environment variables
    to determine which backend to use as the default. It checks for DBSync-related
    variables first, then for Blockfrost, and finally for Ogmios and Kupo variables.
    """
    # Check for DBSync environment variables
    dbsync_vars = [
        "DBSYNC_USER",
        "DBSYNC_PASS",
        "DBSYNC_HOST",
        "DBSYNC_PORT",
        "DBSYNC_DB_NAME",
    ]
    if all(env_var in os.environ for env_var in dbsync_vars):
        set_backend(DbsyncBackend())
        return

    # Check for Blockfrost environment variables
    if "BLOCKFROST_PROJECT_ID" in os.environ:
        set_backend(
            BlockFrostBackend(
                project_id=os.environ["BLOCKFROST_PROJECT_ID"],
            ),
        )
        return

    # Check for Ogmios and Kupo environment variables
    ogmios_kupo_vars = [
        "OGMIOS_URL",
        "KUPO_URL",
        "CARDANO_NETWORK",
    ]
    if all(env_var in os.environ for env_var in ogmios_kupo_vars):
        network = (
            Network.MAINNET
            if os.environ["CARDANO_NETWORK"].upper() == "MAINNET"
            else Network.TESTNET
        )
        set_backend(
            OgmiosKupoBackend(
                ogmios_url=os.environ["OGMIOS_URL"],
                kupo_url=os.environ["KUPO_URL"],
                network=network,
            ),
        )
        return

    # If no backend can be set, log a warning
    logging.warning("No default backend could be set. Please set a backend manually.")


# Initialize the backend when the module is imported
set_default_backend()
