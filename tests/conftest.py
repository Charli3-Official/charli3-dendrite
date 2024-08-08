import pytest

from cardex.dexs.core.base import AbstractPairState

# This grabs all the DEXs
subclass_walk = [AbstractPairState]
D = []

while len(subclass_walk) > 0:
    c = subclass_walk.pop()

    subclasses = c.__subclasses__()

    # If no subclasses, this is a a DEX class. Ignore MuesliCLP for now
    try:
        if isinstance(c.dex(), str) and c.__name__ not in ["MuesliSwapCLPState"]:
            D.append(c)
            subclass_walk.extend(subclasses)
        else:
            subclass_walk.extend(subclasses)
    except NotImplementedError:
        subclass_walk.extend(subclasses)

D = list(sorted(set(D), key=lambda d: d.__name__))

# This sets up each DEX to be selected for testing individually
DEXS = [pytest.param(d, marks=getattr(pytest.mark, d.dex().lower())) for d in D]


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
    dex_names = list(sorted(set([d.dex() for d in D])))

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
    dex_names = list(sorted(set([d.dex().lower() for d in D])))
    if not any([config.getoption(f"--{d}") for d in dex_names]):
        return

    for name in dex_names:
        if not config.getoption(f"--{name}"):
            skip_model = pytest.mark.skip(reason=f"need --{name} option to run")
            for item in items:
                if name in item.keywords:
                    item.add_marker(skip_model)
