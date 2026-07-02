"""Utility functions for assets and Plutus scripts."""

import json
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import requests
from pycardano import Value

from charli3_dendrite.dataclasses.models import Assets

ASSET_PATH = Path(__file__).parent.joinpath(".assets")

ASSET_PATH.mkdir(parents=True, exist_ok=True)


def asset_info(unit: str, update: bool = False) -> dict:  # noqa: ARG001
    """Fetch and cache asset information.

    Args:
        unit (str): The unit of the asset to retrieve information for.
        update (bool): Whether to update the asset info forcefully.

    Returns:
        dict: dictionary containing asset information.
    """
    path = ASSET_PATH.joinpath(f"{unit}.json")

    if path.exists():
        with path.open() as fr:
            parsed = json.load(fr)
            if "timestamp" in parsed and (
                datetime.now() - datetime.fromtimestamp(parsed["timestamp"])
            ) < timedelta(days=1, minutes=0, seconds=0):
                return parsed

    response = requests.get(
        f"https://raw.githubusercontent.com/cardano-foundation/cardano-token-registry/master/mappings/{unit}.json",
        timeout=10,
    )

    if response.status_code != requests.codes.ok:
        msg = f"Error fetching asset info, {unit}: {response.text}"
        raise requests.HTTPError(msg)

    parsed = response.json()
    parsed["timestamp"] = datetime.now().timestamp()
    with path.open("w") as fw:
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

    parsed = asset_info(unit)
    if "decimals" not in parsed:
        return 0

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
    return Value.from_primitive([coin, cnts])


def naturalize_assets(assets: Assets) -> dict[str, Decimal]:
    """Get the number of decimals associated with an asset.

    This returns a `Decimal` with the proper precision context.

    Args:
        assets (Assets): The policy id plus hex encoded name of an asset.

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


def apply_params_to_script(script_cbor: bytes, *params: bytes | int) -> bytes:
    """Apply Plutus ``Data`` parameters to a parameterized Plutus script.

    A pure-Python equivalent of ``aiken blueprint apply`` (via the optional
    ``uplc`` library, imported lazily): the script term is wrapped in an
    ``Apply`` node per parameter and re-serialized, so a parameterized
    validator's deployed script — and hence its on-chain script hash /
    minting-policy id — can be derived without external tooling.

    Parameters are applied left-to-right as ``Data`` constants (``bytes`` -> a
    Data bytestring, ``int`` -> a Data integer). ``script_cbor`` and the return
    value are the CBOR-wrapped flat encodings used by ``PlutusV2Script`` and
    ``plutus.json`` ``compiledCode``.

    Args:
        script_cbor: The un-applied compiled script, CBOR-wrapped.
        *params: The Data parameters to apply, in order.

    Returns:
        The parameter-applied compiled script, CBOR-wrapped.
    """
    from uplc.ast import PlutusByteString  # type: ignore[import-untyped]
    from uplc.ast import PlutusInteger  # type: ignore[import-untyped]
    from uplc.tools import apply  # type: ignore[import-untyped]
    from uplc.tools import flatten  # type: ignore[import-untyped]
    from uplc.tools import unflatten  # type: ignore[import-untyped]

    constants = [
        PlutusByteString(p) if isinstance(p, bytes) else PlutusInteger(p)
        for p in params
    ]
    return flatten(apply(unflatten(script_cbor), *constants))


# Mainnet slot <-> wall-clock anchor (post-Shelley: 1 slot == 1 second). Declared once:
# at absolute slot 56_332_800 the wall-clock time is 1_647_899_091_000 ms (the epoch-328
# boundary). Every other slot's time is this anchor plus the elapsed seconds.
MAINNET_SLOT_ANCHOR = 56_332_800
MAINNET_SLOT_ANCHOR_MS = 1_647_899_091_000


def slot_to_posix_ms(slot: int) -> int:
    """Mainnet absolute slot -> the wall-clock POSIX milliseconds for that slot."""
    return MAINNET_SLOT_ANCHOR_MS + (slot - MAINNET_SLOT_ANCHOR) * 1000


def posix_ms_to_slot(posix_ms: int) -> int:
    """Wall-clock POSIX milliseconds -> the mainnet absolute slot containing them.

    Inverse of :func:`slot_to_posix_ms`: floors to the slot in progress at
    `posix_ms`, so `posix_ms_to_slot(slot_to_posix_ms(slot)) == slot`.
    """
    return MAINNET_SLOT_ANCHOR + (posix_ms - MAINNET_SLOT_ANCHOR_MS) // 1000
