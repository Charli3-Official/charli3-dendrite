import pytest

# from cardex import GeniusYieldOrderState
# from cardex import MinswapCPPState
# from cardex import MinswapDJEDiUSDStableState
# from cardex import MinswapDJEDUSDCStableState
# from cardex import MuesliSwapCLPState
# from cardex import MuesliSwapCPPState
# from cardex import SpectrumCPPState
# from cardex import SundaeSwapCPPState
# from cardex import VyFiCPPState
# from cardex import WingRidersCPPState
# from cardex import WingRidersSSPState
from cardex.backend.dbsync import get_historical_order_utxos
from cardex.backend.dbsync import get_order_utxos_by_block_or_tx
from cardex.dataclasses.models import SwapTransactionInfo
from cardex.dexs.amm.amm_base import AbstractPairState


def test_get_orders(dex: AbstractPairState, benchmark):
    order_selector = dex.order_selector
    result = benchmark(
        get_historical_order_utxos,
        stake_addresses=order_selector,
        limit=10,
    )

    # Test roundtrip parsing
    for ind, r in enumerate(result):
        reparsed = SwapTransactionInfo(r.model_dump())
        assert reparsed == r


@pytest.mark.parametrize("block", [9655329])
def test_get_orders_in_block(block: int, dexs: list[AbstractPairState]):
    order_selector = []
    for dex in dexs:
        order_selector.extend(dex.order_selector)
    orders = get_order_utxos_by_block_or_tx(
        stake_addresses=order_selector, block_no=block
    )

    # Assert requested assets are not empty
    for order in orders:
        for swap in order:
            swap_input = swap.swap_input
            for dex in dexs:
                if swap_input.address_stake in dex.order_selector:
                    try:
                        datum = dex.order_datum_class.from_cbor(swap_input.datum_cbor)
                        break
                    except (DeserializeException, TypeError, AssertionError):
                        continue
                else:
                    continue

            assert "" not in datum.requested_amount()
