from cardex.base import AbstractConstantProductPoolState
from cardex.utility import Assets, InvalidPoolError


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

    def pool_policy() -> str:
        return "0029cb7c88c7567b63d1a512c0ed626aa169688ec980730c0473b91370"

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        assets = values["assets"]
        datum = values["datum"]

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 2000000

        numerator = datum["fields"][-1]["fields"][0]["int"]
        denominator = datum["fields"][-1]["fields"][1]["int"]
        values["fee"] = int(numerator * 10000 / denominator)
