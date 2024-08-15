"""This module contains methods that are backend independent."""

import requests

from charli3_dendrite.dataclasses.models import TokenInfo


def get_token_from_registry(asset: str) -> TokenInfo | None:
    """Get token from the Cardano Token Registry.

    Args:
        asset: The policy + name of the asset.

    Returns:
        Either the token information if the asset exists, or None if it doesn't.
    """
    response = requests.get(
        f"https://raw.githubusercontent.com/cardano-foundation/cardano-token-registry/master/mappings/{asset}.json",
        timeout=15,
    )

    if response.status_code != requests.status_codes.codes.OK:
        return None

    response = response.json()

    ticker = "" if "ticker" not in response else response["ticker"]["value"]
    if ticker == "":
        ticker = bytes.fromhex(asset[56:]).decode(encoding="latin_1")
    name = "" if "name" not in response else response["name"]["value"]
    policy_id = asset[:56]
    policy_name = asset[56:]
    decimals = 0 if "decimals" not in response else response["decimals"]["value"]
    logo = "" if "logo" not in response else response["logo"]["value"]
    return TokenInfo(
        ticker=ticker,
        name=name,
        policy_id=policy_id,
        policy_name=policy_name,
        decimals=decimals,
        logo=logo,
    )
