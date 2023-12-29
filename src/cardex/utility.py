import json
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import requests
from pycardano import Value

from cardex.dataclasses.models import Assets

ASSET_PATH = Path(__file__).parent.joinpath(".assets")

ASSET_PATH.mkdir(parents=True, exist_ok=True)


def asset_info(unit: str, update=False):
    path = ASSET_PATH.joinpath(f"{unit}.json")

    if path.exists():
        with open(path) as fr:
            parsed = json.load(fr)
            if "timestamp" in parsed and (
                datetime.now() - datetime.fromtimestamp(parsed["timestamp"])
            ) < timedelta(days=1, minutes=0, seconds=0):
                return parsed

    response = requests.get(
        f"https://raw.githubusercontent.com/cardano-foundation/cardano-token-registry/master/mappings/{unit}.json",
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


def asset_to_value(assets: Assets) -> Value:
    """Convert an Assets object to a pycardano.Value."""
    coin = assets["lovelace"]
    cnts = {}
    for unit, quantity in assets.items():
        if unit == "lovelace":
            continue
        policy = bytes.fromhex(unit[:56])
        asset_name = bytes.fromhex(unit[56:])
        if policy not in cnts:
            cnts[policy] = {asset_name: quantity}
        else:
            cnts[policy][asset_name] = quantity

    if len(cnts) == 0:
        return Value.from_primitive([coin])
    else:
        return Value.from_primitive([coin, cnts])


def naturalize_assets(assets: Assets) -> dict[str, Decimal]:
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
