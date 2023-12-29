import json
import time
from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import Optional
from typing import Union

import requests
from pycardano import Address
from pycardano import PlutusData
from pydantic import BaseModel
from pydantic import Field

from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm_types import AbstractConstantProductPoolState
from cardex.dexs.errors import NoAssetsError
from cardex.dexs.errors import NotAPoolError
from cardex.utility import Assets


@dataclass
class VyFiPoolDatum(PlutusData):
    """TODO: Figure out what each of these numbers mean."""

    CONSTR_ID = 0

    a: int
    b: int
    c: int

    def pool_pair(self) -> Assets | None:
        return None


@dataclass
class AtoB(PlutusData):
    CONSTR_ID = 3
    min_receive: int


@dataclass
class BtoA(PlutusData):
    CONSTR_ID = 4
    min_receive: int


@dataclass
class VyFiOrderDatum(PlutusData):
    CONSTR_ID = 0
    address: bytes
    order: Union[AtoB, BtoA]

    @classmethod
    def create_datum(
        cls,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets | None = None,
        deposit: Assets | None = None,
        forward_address: Address | None = None,
    ):
        address_hash = (
            address.payment_part.to_primitive() + address.staking_part.to_primitive()
        )

        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            order = AtoB(min_receive=out_assets.quantity())
        else:
            order = BtoA(min_receive=out_assets.quantity())

        return cls(address=address_hash, order=order)


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


class VyFiCPPState(AbstractConstantProductPoolState):
    _batcher = Assets(lovelace=1900000)
    _deposit = Assets(lovelace=2000000)
    _pools: ClassVar[dict[str, VyFiPoolDefinition] | None] = None
    _pools_refresh: ClassVar[float] = time.time()
    lp_fee: int
    bar_fee: int

    @classmethod
    @property
    def dex(cls) -> str:
        return "VyFi"

    @classmethod
    @property
    def pools(cls) -> dict[str, VyFiPoolDefinition]:
        if cls._pools is None or (time.time() - cls._pools_refresh) > 3600:
            cls._pools = {}
            for p in requests.get("https://api.vyfi.io/lp?networkId=1&v2=true").json():
                p["json"] = json.loads(p["json"])
                cls._pools[
                    p["json"]["mainNFT"]["currencySymbol"]
                ] = VyFiPoolDefinition.model_validate(p)
            cls._pools_refresh = time.time()

        return cls._pools

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="addresses",
            selector=[pool.poolValidatorUtxoAddress for pool in cls.pools.values()],
        )

    @property
    def swap_forward(self) -> bool:
        return False

    @property
    def stake_address(self) -> Address:
        return Address.from_primitive(
            VyFiCPPState.pools[self.pool_id].orderValidatorUtxoAddress,
        )

    @classmethod
    @property
    def order_datum_class(self) -> type[VyFiOrderDatum]:
        return VyFiOrderDatum

    @classmethod
    @property
    def pool_datum_class(self) -> type[VyFiPoolDatum]:
        return VyFiPoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @property
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
            assert any([p in cls.pools for p in values["pool_nft"]])
            if isinstance(values["pool_nft"], dict):
                pool_nft = Assets(root=values["pool_nft"])
            else:
                pool_nft = values["pool_nft"]

        # Check for the dex nft
        else:
            nfts = [asset for asset, quantity in assets.items() if asset in cls.pools]
            if len(nfts) < 1:
                if len(assets) == 0:
                    raise NoAssetsError(
                        f"{cls.__name__}: No assets supplied.",
                    )
                else:
                    raise NotAPoolError(
                        f"{cls.__name__}: Pool must have one DEX NFT token.",
                    )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        values["lp_fee"] = cls.pools[pool_nft.unit()].json_.feesSettings.liqFee
        values["bar_fee"] = cls.pools[pool_nft.unit()].json_.feesSettings.barFee

        return pool_nft
