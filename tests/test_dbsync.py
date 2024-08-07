import pytest
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

# Set up the backend for all DEX classes
backend = DbsyncBackend()
dex_classes = [
    MinswapCPPState,
    MinswapDJEDiUSDStableState,
    MinswapDJEDUSDCStableState,
    MinswapDJEDUSDMStableState,
    MinswapV2CPPState,
    SundaeSwapV3CPPState,
    WingRidersSSPState,
]

for dex_class in dex_classes:
    dex_class.set_backend(backend)


@pytest.mark.parametrize("n_blocks", range(1, 5))
def test_last_blocks(n_blocks: int):
    result = backend.last_block(n_blocks)
    assert len(result) == n_blocks


@pytest.mark.parametrize(
    "n_blocks",
    range(1, 14, 2),
    ids=[f"blocks={2**n}" for n in range(1, 14, 2)],
)
def test_last_blocks_benchmark(n_blocks: int, benchmark):
    result = benchmark(backend.last_block, 2**n_blocks)


def test_get_pool_utxos(dex: AbstractPoolState, run_slow: bool, benchmark):
    if issubclass(dex, AbstractOrderBookState):
        return

    selector = dex.pool_selector
    limit = 20000 if run_slow else 100
    result = benchmark(
        backend.get_pool_utxos,
        limit=limit,
        historical=False,
        **selector.to_dict(),
    )

    assert len(result) < 20000
    if dex in [
        MinswapDJEDiUSDStableState,
        MinswapDJEDUSDCStableState,
        MinswapDJEDUSDMStableState,
    ]:
        assert len(result) == 1
    elif dex == WingRidersSSPState:
        assert len(result) == 3
    elif dex == SundaeSwapV3CPPState:
        assert len(result) > 35
    else:
        assert len(result) > 40


def test_get_pool_script_version(dex: AbstractPoolState, benchmark):
    if issubclass(dex, AbstractOrderBookState):
        return

    selector = dex.pool_selector
    result = benchmark(
        backend.get_pool_utxos,
        limit=1,
        historical=False,
        **selector.to_dict(),
    )
    if dex.dex in ["Spectrum"] or dex in [
        MinswapDJEDiUSDStableState,
        MinswapDJEDUSDCStableState,
        MinswapDJEDUSDMStableState,
        SundaeSwapV3CPPState,
        MinswapV2CPPState,
    ]:
        assert result[0].plutus_v2
    else:
        assert not result[0].plutus_v2


def test_get_orders(dex: AbstractPoolState, run_slow: bool, benchmark):
    if issubclass(dex, AbstractOrderBookState):
        return

    limit = 10 if run_slow else 1000

    order_selector = dex.order_selector
    result = benchmark(
        backend.get_historical_order_utxos,
        stake_addresses=order_selector,
        limit=limit,
    )


@pytest.mark.parametrize(
    "tx_hash",
    ["ec77a0fcbbe03e3ab04f609dc95eb731334c8508a2c03b00c31c8de89688e04b"],
)
def test_get_pool_in_tx(tx_hash):
    selector = MinswapCPPState.pool_selector
    tx = backend.get_pool_in_tx(tx_hash=tx_hash, **selector.to_dict())
    assert len(tx) > 0
