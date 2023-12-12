from dataclasses import dataclass

from pycardano import PlutusData

from cardex.base import AbstractConstantProductPoolState
from cardex.utility import Assets, AssetClass, InvalidPoolError, NotAPoolError


@dataclass
class LPFee(PlutusData):
    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class LiquidityPoolAssets(PlutusData):
    CONSTR_ID = 0
    asset_a: AssetClass
    asset_b: AssetClass


@dataclass
class LiquidityPoolDatum(PlutusData):
    CONSTR_ID = 0
    assets: LiquidityPoolAssets
    ident: bytes
    last_swap: int
    fee: LPFee


class SundaeswapCPPState(AbstractConstantProductPoolState):
    _dex = "sundaeswap"
    _batcher = Assets(lovelace=2500000)
    _deposit = Assets(lovelace=2000000)

    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values) -> bool:
        if "pool_nft" in values and "dex_nft" in values:
            if len(values["assets"]) == 3:
                # Send the ADA token to the end
                values["assets"]["lovelace"] = values["assets"].pop("lovelace")
            values["assets"] = Assets.model_validate(values["assets"])
            return True
        else:
            return False

    @classmethod
    def extract_pool_nft(cls, values) -> Assets:
        try:
            super().extract_pool_nft(values)
        except NotAPoolError:
            raise InvalidPoolError("No pool NFT found.")

    def pool_policy() -> str:
        return "0029cb7c88c7567b63d1a512c0ed626aa169688ec980730c0473b91370"

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        assets = values["assets"]
        datum = LiquidityPoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 2000000

        numerator = datum.fee.numerator
        denominator = datum.fee.denominator
        values["fee"] = int(numerator * 10000 / denominator)
