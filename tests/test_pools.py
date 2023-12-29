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
from cardex.dexs.amm_base import AbstractPoolState
from cardex.dexs.errors import InvalidLPError
from cardex.dexs.errors import InvalidPoolError
from cardex.dexs.errors import NoAssetsError

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
def test_pools_script_version(dex: AbstractPoolState, subtests):
    selector = dex.pool_selector
    result = get_pool_utxos(limit=1, historical=False, **selector.to_dict())

    counts = 0
    for pool in result:
        with subtests.test(f"Testing: {dex.dex}", i=pool):
            try:
                dex.model_validate(pool.model_dump())
                counts += 1
            except InvalidLPError:
                pytest.xfail(
                    f"{dex.dex}: expected failure lp tokens were not found or invalid - {pool.assets}",
                )
            except NoAssetsError:
                pytest.xfail(f"{dex.dex}: expected failure no assets - {pool.assets}")
            except InvalidPoolError:
                pytest.xfail(f"{dex.dex}: expected failure no pool NFT - {pool.assets}")
            except:
                raise


@pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
def test_parse_pools(dex: AbstractPoolState, subtests):
    selector = dex.pool_selector
    result = get_pool_utxos(limit=10000, historical=False, **selector.to_dict())

    counts = 0
    for pool in result:
        with subtests.test(f"Testing: {dex.dex}", i=pool):
            try:
                dex.model_validate(pool.model_dump())
                counts += 1
            except InvalidLPError:
                pytest.xfail(
                    f"{dex.dex}: expected failure lp tokens were not found or invalid - {pool.assets}",
                )
            except NoAssetsError:
                pytest.xfail(f"{dex.dex}: expected failure no assets - {pool.assets}")
            except InvalidPoolError:
                pytest.xfail(f"{dex.dex}: expected failure no pool NFT - {pool.assets}")
            except:
                raise

    assert counts < 10000
    if dex == WingRidersSSPState:
        assert counts == 2
    elif dex == MuesliSwapCLPState:
        assert counts <= 16
    else:
        assert counts > 50
