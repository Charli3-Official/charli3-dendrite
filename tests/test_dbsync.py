import pytest
from cardex import MinswapCPPState
from cardex import MuesliSwapCLPState
from cardex import MuesliSwapCPPState
from cardex import SpectrumCPPState
from cardex import SundaeSwapCPPState
from cardex import VyFiCPPState
from cardex import WingRidersCPPState
from cardex import WingRidersSSPState
from cardex.backend.dbsync import get_order_utxos
from cardex.backend.dbsync import get_pool_in_tx
from cardex.backend.dbsync import get_pool_utxos
from cardex.backend.dbsync import last_block
from cardex.dexs.amm_base import AbstractPoolState

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

    assert len(result) < 9000
    if dex == WingRidersSSPState:
        assert len(result) == 2
    elif dex == MuesliSwapCLPState:
        assert len(result) == 16
    else:
        assert len(result) > 50


@pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
def test_get_pool_script_version(dex: AbstractPoolState, benchmark):
    selector = dex.pool_selector
    result = benchmark(
        get_pool_utxos,
        limit=1,
        historical=False,
        **selector.to_dict(),
    )
    if dex.dex in ["Spectrum"]:
        assert result[0].plutus_v2
    else:
        assert not result[0].plutus_v2


@pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
def test_get_orders(dex: AbstractPoolState, benchmark):
    order_selector = dex.order_selector
    result = benchmark(
        get_order_utxos,
        stake_addresses=order_selector,
        limit=1000,
    )

    assert len(result) == 1000


@pytest.mark.parametrize(
    "tx_hash",
    ["ec77a0fcbbe03e3ab04f609dc95eb731334c8508a2c03b00c31c8de89688e04b"],
)
def test_get_pool_in_tx(tx_hash):
    selector = MinswapCPPState.pool_selector
    tx = get_pool_in_tx(tx_hash=tx_hash, **selector.to_dict())

    assert len(tx) > 0
