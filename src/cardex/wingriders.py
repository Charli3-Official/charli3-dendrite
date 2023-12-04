from cardex.base import AbstractConstantProductPoolState
from cardex.utility import Assets, InvalidPoolError

try:
    from minswap.utils import BlockfrostBackend

    BLOCKFROST = True
except ImportError:
    BLOCKFROST = False


class WingridersCPPState(AbstractConstantProductPoolState):
    _dex = "wingriders"
    fee: int = 35
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)

    def pool_policy() -> str:
        return "026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a570"

    def dex_policy() -> str:
        return "026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a5704c"

    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.lp_tokens.unit()

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
        if "pool_nft" in values:
            return Assets()

        assets = values["assets"]

        # Find the NFT that assigns the pool a unique id
        nfts = [asset for asset in assets if asset.startswith(cls.pool_policy())]
        if len(nfts) != 1:
            raise InvalidPoolError(
                f"A pool must have one at least one LP token: {nfts}"
            )
        assets.root.pop(nfts[0])

        return Assets()

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        assets = values["assets"]
        datum = values["datum"]

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 3000000

        assets.root[assets.unit(0)] -= datum["fields"][1]["fields"][2]["int"]
        assets.root[assets.unit(1)] -= datum["fields"][1]["fields"][3]["int"]
