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
from cardex.dataclasses.models import SwapTransactionInfo
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


@pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
def test_get_orders(dex: AbstractPoolState, benchmark):
    order_selector = dex.order_selector
    result = benchmark(
        get_order_utxos,
        stake_addresses=order_selector,
        limit=1000,
    )

    # Test roundtrip parsing
    for ind, r in enumerate(result):
        reparsed = SwapTransactionInfo(r.model_dump())
        assert reparsed == r
