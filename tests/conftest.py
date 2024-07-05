import pytest
from cardex.dexs.core.base import AbstractPairState

# This grabs all the DEXs
subclass_walk = [AbstractPairState]
D = []

tests_db_sync_success = [
    "SpectrumCPPState",
    "SundaeSwapCPPState",
    "SundaeSwapV3CPPState",
    "MuesliSwapCPPState",
    "WingRidersSSPState",
    "WingRidersCPPState",
    "MinswapCPPState",
    "MinswapDJEDUSDCStableState",
    "MinswapDJEDUSDMStableState",
    "MinswapDJEDiUSDStableState",
    "VyFiCPPState",
    "GeniusYieldOrderState",
    "GeniusYieldOrderBook",
]

tests_amm_success = [
    "MinswapCPPState",
    "SpectrumCPPState",
    "MinswapDJEDUSDMStableState",
    "MinswapDJEDiUSDStableState",
    "MinswapDJEDUSDCStableState",
    "SundaeSwapCPPState",
    "SundaeSwapV3CPPState",
    "WingRidersSSPState",
    "WingRidersCPPState",
    "VyFiCPPState",
    "MuesliSwapCPPState",
    "GeniusYieldOrderBook",
    "GeniusYieldOrderState",  # Tests for GeniusYieldOrderState Failing
]

tests_utxo_success = [
    # "GeniusYieldOrderBook",
    # "MuesliSwapCPPState",
    # "SundaeSwapCPPState",
    # "WingRidersSSPState",
    # "MinswapCPPState",
    # "VyFiCPPState",
    # "WingRidersCPPState",
    # "SpectrumCPPState",
    # "SundaeSwapV3CPPState",
    # "MinswapDJEDUSDCStableState"
]

tests_utxo_failed = [
    "MinswapDJEDUSDMStableState",
    "MinswapDJEDiUSDStableState",
    "GeniusYieldOrderState",
]

while len(subclass_walk) > 0:
    c = subclass_walk.pop()

    subclasses = c.__subclasses__()

    try:
        # Try calling the dex method
        if (
            isinstance(c.dex(), str)
            and c.__name__ not in ["MuesliSwapCLPState"]
            and c.__name__ not in tests_utxo_failed
        ):
            D.append(c)
    except NotImplementedError:
        # Skip if the method is not implemented
        subclass_walk.extend(subclasses)
    except TypeError:
        # Skip if dex is not a callable method
        pass
    else:
        subclass_walk.extend(subclasses)

D = list(set(D))

# This sets up each DEX to be selected for testing individually
DEXS = [pytest.param(d, marks=getattr(pytest.mark, d.dex.__name__.lower())) for d in D]


@pytest.fixture(scope="module", params=DEXS)
def dex(request) -> AbstractPairState:
    """Autogenerate a list of all DEX classes.

    Returns:
        List of all DEX classes. This could be a full order book, an individual order,
        a stableswap, or a constant product pool class.
    """

    return request.param


@pytest.fixture
def dexs() -> list[AbstractPairState]:
    """A list of all DEXs."""
    return D


@pytest.fixture
def run_slow(request) -> bool:
    """A list of all DEXs."""
    return request.config.getoption("--slow")


def pytest_addoption(parser):
    """Add pytest configuration options."""
    dex_names = list(set([d.dex.__name__ for d in D]))

    for name in dex_names:
        parser.addoption(
            f"--{name.lower()}",
            action="store_true",
            default=False,
            help=f"run tests for {name}",
        )

    parser.addoption(
        f"--slow",
        action="store_true",
        default=False,
        help=f"run full battery of tests",
    )


def pytest_collection_modifyitems(config, items):
    """Modify tests based on command line arguments."""
    dex_names = list(set([d.dex.__name__.lower() for d in D]))
    if not any([config.getoption(f"--{d}") for d in dex_names]):
        return

    for name in dex_names:
        if not config.getoption(f"--{name}"):
            skip_model = pytest.mark.skip(reason=f"need --{name} option to run")
            for item in items:
                if name in item.keywords:
                    item.add_marker(skip_model)
