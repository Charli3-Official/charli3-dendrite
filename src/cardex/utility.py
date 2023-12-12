import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pycardano
import requests
from pydantic import BaseModel, RootModel, root_validator

ASSET_PATH = Path(__file__).parent.joinpath(".assets")

ASSET_PATH.mkdir(parents=True, exist_ok=True)


class NotAPoolError(Exception):
    pass


class InvalidPoolError(Exception):
    pass


class BaseList(RootModel):
    """Utility class for list models."""

    def __iter__(self):  # noqa
        return iter(self.root)

    def __getitem__(self, item):  # noqa
        return self.root[item]

    def __len__(self):  # noqa
        return len(self.root)


class BaseDict(BaseList):
    """Utility class for dict models."""

    def items(self):
        """Return iterable of key-value pairs."""
        return self.root.items()

    def keys(self):
        """Return iterable of keys."""
        return self.root.keys()

    def values(self):
        """Return iterable of values."""
        return self.root.values()

    def __getitem__(self, item):  # noqa
        return self.root.get(item, 0)


class Assets(BaseDict):
    """Contains all tokens and quantities."""

    root: Dict[str, int] = {}

    def unit(self, index: int = 0) -> str:
        """Units of asset at `index`."""
        return list(self.keys())[index]

    def quantity(self, index: int = 0) -> int:
        """Quantity of the asset at `index`."""
        return list(self.values())[index]

    @root_validator(pre=True)
    def _digest_assets(cls, values):
        if hasattr(values, "root"):
            root = values.root
        elif "values" in values and isinstance(values["values"], list):
            root = {v.unit: v.quantity for v in values["values"]}
        else:
            root = {k: v for k, v in values.items()}
        root = dict(
            sorted(root.items(), key=lambda x: "" if x[0] == "lovelace" else x[0])
        )

        return {"root": root}

    def __add__(a, b):
        """Add two assets."""
        intersection = set(a.keys()) | set(b.keys())

        result = {key: a[key] + b[key] for key in intersection}

        return Assets(**result)

    def __sub__(a, b):
        """Subtract two assets."""
        intersection = set(a.keys()) | set(b.keys())

        result = {key: a[key] - b[key] for key in intersection}

        return Assets(**result)


@dataclass
class AssetClass(pycardano.PlutusData):
    """An asset class. Separates out token policy and asset name."""

    CONSTR_ID = 0

    policy: bytes
    asset_name: bytes

    @classmethod
    def from_assets(cls, asset: Assets):
        """Parse an Assets object into an AssetClass object."""
        assert len(asset) == 1

        if asset.unit() == "lovelace":
            return AssetClass(
                policy=b"",
                asset_name=b"",
            )
        else:
            return AssetClass(
                policy=bytes.fromhex(asset.unit()[:56]),
                asset_name=bytes.fromhex(asset.unit()[56:]),
            )


def asset_info(unit: str, update=False):
    path = ASSET_PATH.joinpath(f"{unit}.json")

    if path.exists():
        with open(path, "r") as fr:
            parsed = json.load(fr)
            if "timestamp" in parsed and (
                datetime.now() - datetime.fromtimestamp(parsed["timestamp"])
            ) < timedelta(days=1, minutes=0, seconds=0):
                return parsed

    response = requests.get(
        f"https://raw.githubusercontent.com/cardano-foundation/cardano-token-registry/master/mappings/{unit}.json"
    )

    if response.status_code != 200:
        raise requests.HTTPError(f"Error fetching asset info, {unit}: {response.text}")

    parsed = response.json()
    parsed["timestamp"] = datetime.now().timestamp()
    with open(path, "w") as fw:
        json.dump(response.json(), fw)

    return response.json()


def asset_decimals(unit: str) -> int:
    """Asset decimals.

    All asset quantities are stored as integers. The decimals indicates a scaling factor
    for the purposes of human readability of asset denominations.

    For example, ADA has 6 decimals. This means every 10**6 units (lovelace) is 1 ADA.

    Args:
        unit: The policy id plus hex encoded name of an asset.

    Returns:
        The decimals for the asset.
    """
    if unit == "lovelace":
        return 6
    else:
        parsed = asset_info(unit)
        if "decimals" not in parsed:
            return 0
        else:
            return int(parsed["decimals"]["value"])


def asset_ticker(unit: str) -> str:
    """Ticker symbol for an asset.

    This function is designed to always return a value. If a `ticker` is available in
    the asset metadata, it is returned. Otherwise, the human readable asset name is
    returned.

    Args:
        unit: The policy id plus hex encoded name of an asset.

    Returns:
        The ticker or human readable name of an asset.
    """
    if unit == "lovelace":
        asset_ticker = "ADA"
    else:
        parsed = asset_info(unit)

        if "ticker" in parsed:
            asset_ticker = parsed["ticker"]["value"]
        else:
            asset_ticker = bytes.fromhex(unit[56:]).decode()

    return asset_ticker


def asset_name(unit: str) -> str:
    """Ticker symbol for an asset.

    This function is designed to always return a value. If a `ticker` is available in
    the asset metadata, it is returned. Otherwise, the human readable asset name is
    returned.

    Args:
        unit: The policy id plus hex encoded name of an asset.

    Returns:
        The ticker or human readable name of an asset.
    """
    if unit == "lovelace":
        asset_name = "ADA"
    else:
        parsed = asset_info(unit)

        if "name" in parsed:
            asset_name = parsed["name"]["value"]
        else:
            asset_name = bytes.fromhex(unit[56:]).decode()

    return asset_name


def naturalize_assets(assets: Assets) -> Dict[str, Decimal]:
    """Get the number of decimals associated with an asset.

    This returns a `Decimal` with the proper precision context.

    Args:
        asset: The policy id plus hex encoded name of an asset.

    Returns:
        A dictionary where assets are keys and values are `Decimal` objects containing
            exact quantities of the asset, accounting for asset decimals.
    """
    nat_assets = {}
    for unit, quantity in assets.items():
        if unit == "lovelace":
            nat_assets["lovelace"] = Decimal(quantity) / Decimal(10**6)
        else:
            nat_assets[unit] = Decimal(quantity) / Decimal(10 ** asset_decimals(unit))

    return nat_assets
