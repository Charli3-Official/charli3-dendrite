import pytest

from cardex import MinswapCPPState
from cardex import MinswapDJEDiUSDStableState
from cardex import MinswapDJEDUSDCStableState
from cardex import MinswapDJEDUSDMStableState
from cardex import MinswapV2CPPState
from cardex import SundaeSwapV3CPPState
from cardex import WingRidersSSPState
from cardex.backend.dbsync.orders import get_cancel_utxos
from cardex.backend.dbsync.orders import get_historical_order_utxos
from cardex.backend.dbsync.pools import get_pool_utxos
from cardex.backend.dbsync.utils import last_block
from cardex.dexs.amm.amm_base import AbstractPoolState
from cardex.dexs.ob.ob_base import AbstractOrderBookState


@pytest.mark.parametrize("n_blocks", range(1, 5))
def test_last_blocks(n_blocks: int):
    result = last_block(n_blocks)

    assert len(result) == n_blocks


@pytest.mark.parametrize(
    "n_blocks",
    range(1, 14, 2),
    ids=[f"blocks={2**n}" for n in range(1, 14, 2)],
)
def test_last_blocks(n_blocks: int, benchmark):
    result = benchmark(last_block, 2**n_blocks)


def test_get_pool_script_version(dex: AbstractPoolState, benchmark):
    if issubclass(dex, AbstractOrderBookState):
        return

    selector = dex.pool_selector()
    result = benchmark(
        get_pool_utxos,
        limit=1,
        historical=False,
        **selector.model_dump(),
    )
    if dex.dex() in ["Spectrum"] or dex in [
        MinswapDJEDiUSDStableState,
        MinswapDJEDUSDCStableState,
        MinswapDJEDUSDMStableState,
        SundaeSwapV3CPPState,
        MinswapV2CPPState,
    ]:
        assert result[0].plutus_v2
    else:
        assert not result[0].plutus_v2
