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
from cardex.dexs.amm.amm_base import AbstractPoolState
from cardex.dexs.core.errors import InvalidLPError
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError

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

MALFORMED_CBOR = {
    "fadbbeb0012ae3864927e523f73048b22fba71d8be6f6a1336561363d3ec0b71",
    "9769d480c4022b36d62a16c7cea8037da7dc1197110a44e3e45104c27577d640",
    "0f6b5410f69646ccd94db7574b02a55e442e008dcdd1e0aceda7aa59d8b7c9ff",
    "44acc41c20c2a25e1f1bcdb0bfeb88d92e26af00d3246f9a163c0b33b2339986",
    "87e8e234b46a2bff09d88b308f7fec72954fc3689d99a093a02d970c3939191d",
    "c503c645047674f62a590164eab4c56f0e2af53fe579ef27c16b1a2ce60cc261",
    "5d0565927717a6de040c33f7b603d416935a24911c5376e3a81d1f74b339f15a",
}


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
            except NotAPoolError as e:
                # Known failures due to malformed data
                if pool.tx_hash in MALFORMED_CBOR:
                    pytest.xfail("Malformed CBOR tx.")
                else:
                    raise
            except:
                raise

    assert counts < 10000
    if dex == WingRidersSSPState:
        assert counts == 2
    elif dex == MuesliSwapCLPState:
        assert counts <= 16
    else:
        assert counts > 50