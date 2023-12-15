from cardex.dexs.abstract_classes import AbstractConstantProductPoolState
from cardex.utility import Assets


class MinswapCPPState(AbstractConstantProductPoolState):
    _dex = "minswap"
    fee: int = 30
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)

    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    def pool_policy() -> str:
        return "0be55d262b29f564998ff81efe21bdc0022621c12f15af08d0f2ddb1"

    def lp_policy() -> str:
        return "e4214b7cce62ac6fbba385d164df48e157eae5863521b4b67ca71d86"

    def dex_policy() -> str:
        return "13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f"
