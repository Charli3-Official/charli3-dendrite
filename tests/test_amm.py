import pytest

from cardex import MinswapDJEDiUSDStableState
from cardex import MinswapDJEDUSDCStableState
from cardex import MinswapDJEDUSDMStableState
from cardex import SundaeSwapV3CPPState
from cardex import WingRidersSSPState
from cardex.dexs.amm.amm_base import AbstractPoolState
from cardex.dexs.ob.ob_base import AbstractOrderBookState
from cardex.dexs.core.errors import InvalidLPError
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError

MALFORMED_CBOR = {
    "fadbbeb0012ae3864927e523f73048b22fba71d8be6f6a1336561363d3ec0b71",
    "9769d480c4022b36d62a16c7cea8037da7dc1197110a44e3e45104c27577d640",
    "0f6b5410f69646ccd94db7574b02a55e442e008dcdd1e0aceda7aa59d8b7c9ff",
    "44acc41c20c2a25e1f1bcdb0bfeb88d92e26af00d3246f9a163c0b33b2339986",
    "87e8e234b46a2bff09d88b308f7fec72954fc3689d99a093a02d970c3939191d",
    "c503c645047674f62a590164eab4c56f0e2af53fe579ef27c16b1a2ce60cc261",
    "5d0565927717a6de040c33f7b603d416935a24911c5376e3a81d1f74b339f15a",
}


def test_pools_script_version(dex: AbstractPoolState, subtests):
    if issubclass(dex, AbstractOrderBookState):
        return

    selector = dex.pool_selector
    result = AbstractPoolState.get_backend().get_pool_utxos(
        limit=1, historical=False, **selector.to_dict()
    )

    counts = 0
    for pool in result:
        try:
            dex.model_validate(pool.model_dump())
            counts += 1
        except (InvalidLPError, NoAssetsError, InvalidPoolError):
            pass
        except:
            raise


def test_parse_pools(dex: AbstractPoolState, run_slow: bool, subtests):
    if issubclass(dex, AbstractOrderBookState):
        return

    selector = dex.pool_selector
    limit = 20000 if run_slow else 100
    result = AbstractPoolState.get_backend().get_pool_utxos(
        limit=limit, historical=False, **selector.to_dict()
    )

    counts = 0
    for pool in result:
        try:
            dex.model_validate(pool.model_dump())
            counts += 1
        except (InvalidLPError, NoAssetsError, InvalidPoolError):
            pass
        except NotAPoolError as e:
            # Known failures due to malformed data
            if pool.tx_hash in MALFORMED_CBOR:
                pass
            else:
                raise
        except:
            raise

    assert counts < 20000
    if dex in [
        MinswapDJEDiUSDStableState,
        MinswapDJEDUSDCStableState,
        MinswapDJEDUSDMStableState,
    ]:
        assert counts == 1
    elif dex == WingRidersSSPState:
        assert counts == 3
    elif dex == SundaeSwapV3CPPState:
        assert counts > 30
    else:
        assert counts > 40
