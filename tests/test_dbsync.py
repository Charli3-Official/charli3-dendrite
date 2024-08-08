import pytest
from cardex.backend import set_backend, get_backend
from cardex.backend.dbsync import DbsyncBackend
from cardex import (
    MinswapCPPState,
    MinswapDJEDiUSDStableState,
    MinswapDJEDUSDCStableState,
    MinswapDJEDUSDMStableState,
    MinswapV2CPPState,
    SundaeSwapV3CPPState,
    WingRidersSSPState,
)
from cardex.dexs.amm.amm_base import AbstractPoolState
from cardex.dexs.ob.ob_base import AbstractOrderBookState

# Set up the backend for all tests
set_backend(DbsyncBackend())


@pytest.mark.parametrize("n_blocks", range(1, 5))
def test_last_blocks(n_blocks: int):
    result = get_backend().last_block(n_blocks)
    assert len(result) == n_blocks


@pytest.mark.parametrize(
    "n_blocks",
    range(1, 14, 2),
    ids=[f"blocks={2**n}" for n in range(1, 14, 2)],
)
def test_last_blocks_benchmark(n_blocks: int, benchmark):
    result = benchmark(get_backend().last_block, 2**n_blocks)
