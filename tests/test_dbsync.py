import pytest
from cardex import MinswapCPPState
from cardex import MuesliSwapCLPState
from cardex import MuesliSwapCPPState
from cardex import SpectrumCPPState
from cardex import SundaeSwapCPPState
from cardex import VyFiCPPState
from cardex import WingRidersCPPState
from cardex import WingRidersSSPState
from cardex.backend.dbsync import get_pool_utxos
from cardex.backend.dbsync import last_block
from cardex.dexs.abstract_classes import AbstractPoolState

DEXS: list[AbstractPoolState] = [
    MinswapCPPState,
    MuesliSwapCLPState,
    MuesliSwapCPPState,
    SpectrumCPPState,
    SundaeSwapCPPState,
    VyFiCPPState,
    WingRidersCPPState,
    WingRidersSSPState,
]


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


@pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
def test_get_pool_utxos(dex: AbstractPoolState, benchmark):
    selector = dex.pool_selector
    result = benchmark(
        get_pool_utxos,
        limit=10000,
        historical=False,
        **selector.to_dict(),
    )

    assert len(result) < 10000
    if dex == WingRidersSSPState:
        assert len(result) == 2
    elif dex == MuesliSwapCLPState:
        assert len(result) == 16
    else:
        assert len(result) > 50
