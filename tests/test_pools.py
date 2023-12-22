import pytest
from cardex import MinswapCPPState
from cardex import MuesliSwapCLPState
from cardex import MuesliSwapCPPState
from cardex import SpectrumCPPState
from cardex import SundaeSwapCPPState
from cardex import VyFiCPPState
from cardex import WingRidersCPPState
from cardex import WingRidersSSPState
from cardex.backend.dbsync import get_pool_in_tx
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


# @pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
# def test_parse_pools(dex: AbstractPoolState, subtests):
#     selector = dex.pool_selector
#     result = get_pool_utxos(limit=10000, historical=False, **selector.to_dict())

#     counts = 0
#     for pool in result:
#         with subtests.test(f"Testing: {dex.dex}", i=pool):
#             try:
#                 dex(
#                     tx_hash=pool.tx_hash,
#                     tx_index=pool.tx_index,
#                     block_time=pool.block_time,
#                     assets=pool.assets,
#                     datum_hash=pool.datum_hash,
#                     datum_cbor=pool.datum_cbor,
#                 )
#                 counts += 1
#             except InvalidLPError:
#                 pytest.xfail(
#                     f"{dex.dex}: expected failure lp tokens were not found or invalid - {pool.assets}",
#                 )
#             except NoAssetsError:
#                 pytest.xfail(f"{dex.dex}: expected failure no assets - {pool.assets}")
#             except InvalidPoolError:
#                 pytest.xfail(f"{dex.dex}: expected failure no pool NFT - {pool.assets}")
#             except:
#                 raise

#     assert counts < 10000
#     if dex == WingRidersSSPState:
#         assert counts == 2
#     elif dex == MuesliSwapCLPState:
#         assert counts == 16
#     else:
#         assert counts > 50


@pytest.mark.parametrize(
    "tx_hash",
    ["ec77a0fcbbe03e3ab04f609dc95eb731334c8508a2c03b00c31c8de89688e04b"],
)
def test_parse_bad_minswap_tx(tx_hash: str):
    selector = MinswapCPPState.pool_selector
    tx = get_pool_in_tx(tx_hash=tx_hash, **selector.to_dict())

    print(len(tx))

    pool = MinswapCPPState.model_validate(tx[0].model_dump())

    print(pool)

    raise Exception
