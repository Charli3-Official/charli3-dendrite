import requests

from cardex.dataclasses.models import TokenInfo


def get_token_from_registry(asset: str) -> TokenInfo | None:
    response = requests.get(
        f"https://raw.githubusercontent.com/cardano-foundation/cardano-token-registry/master/mappings/{asset}.json",
    )

    if response.status_code != 200:
        return None

    response = response.json()

    ticker = "" if "ticker" not in response else response["ticker"]["value"]
    if ticker == "":
        ticker = bytes.fromhex(asset[56:]).decode(encoding="latin_1")
    name = "" if "name" not in response else response["name"]["value"]
    policyId = asset[:56]
    policyName = asset[56:]
    decimals = 0 if "decimals" not in response else response["decimals"]["value"]
    logo = "" if "logo" not in response else response["logo"]["value"]
    return TokenInfo(
        ticker=ticker,
        name=name,
        policy_id=policyId,
        policy_name=policyName,
        decimals=decimals,
        logo=logo,
    )
