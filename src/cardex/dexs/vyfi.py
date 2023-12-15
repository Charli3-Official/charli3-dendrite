import json
from typing import Any
from typing import Optional

import requests
from pydantic import BaseModel
from pydantic import Field

from cardex.dexs.abstract_classes import AbstractConstantProductPoolState
from cardex.utility import Assets
from cardex.utility import InvalidPoolError


class VyFiTokenDefinition(BaseModel):
    tokenName: str
    currencySymbol: str


class VyFiFees(BaseModel):
    barFee: int
    processFee: int
    liqFee: int


class VyFiPoolTokens(BaseModel):
    aAsset: VyFiTokenDefinition
    bAsset: VyFiTokenDefinition
    mainNFT: VyFiTokenDefinition
    operatorToken: VyFiTokenDefinition
    lpTokenName: dict[str, str]
    feesSettings: VyFiFees
    stakeKey: Optional[str]


class VyFiPoolDefinition(BaseModel):
    unitsPair: str
    poolValidatorUtxoAddress: str
    lpPolicyId_assetId: str = Field(alias="lpPolicyId-assetId")
    json_: VyFiPoolTokens = Field(alias="json")
    pair: str
    isLive: bool
    orderValidatorUtxoAddress: str


POOLS = {}
for p in requests.get("https://api.vyfi.io/lp?networkId=1&v2=true").json():
    p["json"] = json.loads(p["json"])
    POOLS[p["json"]["mainNFT"]["currencySymbol"]] = VyFiPoolDefinition.model_validate(p)


class VyFiCPPState(AbstractConstantProductPoolState):
    _dex = "vyfi"
    _batcher = Assets(lovelace=1900000)
    _deposit = Assets(lovelace=2000000)
    lp_fee: int
    bar_fee: int

    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    def volume_fee(self) -> int:
        return self.lp_fee + self.bar_fee

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Optional[Assets]:
        """Extract the dex nft from the UTXO.

        Some DEXs put a DEX nft into the pool UTXO.

        This function checks to see if the DEX nft is in the UTXO if the DEX policy is
        defined.

        If the dex nft is in the values, this value is skipped because it is assumed
        that this utxo has already been parsed.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the dex nft.
        """
        assets = values["assets"]

        # If the dex nft is in the values, it's been parsed already
        if "pool_nft" in values:
            assert any([p in POOLS for p in values["pool_nft"]])
            pool_nft = values["pool_nft"]

        # Check for the dex nft
        else:
            nfts = [asset for asset, quantity in assets.items() if asset in POOLS]
            if len(nfts) < 1:
                raise InvalidPoolError(
                    f"{cls.__name__}: Pool must have one DEX NFT token.",
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        values["lp_fee"] = POOLS[pool_nft.unit()].json_.feesSettings.liqFee
        values["bar_fee"] = POOLS[pool_nft.unit()].json_.feesSettings.barFee

        return pool_nft
