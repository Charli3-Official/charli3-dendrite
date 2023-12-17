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
from cardex.dexs.abstract_classes import AbstractPoolState
from cardex.utility import InvalidLPError
from cardex.utility import NoAssetsError

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
def test_parse_pools(dex: AbstractPoolState, subtests):
    selector = dex.pool_selector
    result = get_pool_utxos(limit=10000, historical=False, **selector.to_dict())

    for pool in result:
        with subtests.test(f"Testing: {dex.dex}", i=pool):
            try:
                dex(
                    tx_hash=pool.tx_hash,
                    tx_index=pool.tx_index,
                    block_time=pool.block_time,
                    assets=pool.assets,
                    datum_hash=pool.datum_hash,
                    datum_cbor=pool.datum_cbor,
                )
            except InvalidLPError:
                pytest.xfail(
                    f"{dex.dex}: expected failure lp tokens were not found or invalid - {pool.assets}",
                )
            except NoAssetsError:
                pytest.xfail(f"{dex.dex}: expected failure no assets - {pool.assets}")
            except:
                print(pool)
                raise
