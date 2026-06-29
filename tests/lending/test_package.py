import subprocess
import sys

import charli3_dendrite.lending as lending
from charli3_dendrite.dexs.core.base import AbstractPairState
from charli3_dendrite.lending import AbstractLendingPoolState
from charli3_dendrite.lending import AbstractLoanState
from charli3_dendrite.lending import LendingBook
from charli3_dendrite.lending import LendingPriceBook
from charli3_dendrite.lending import OraclePrice
from charli3_dendrite.lending.oracles.models import CORE_ORACLE_SOURCES
from charli3_dendrite.lending.oracles.models import resolver_sources


def test_core_sources_registered_on_import():
    # Core (protocol-agnostic) sources are always registered after importing lending.
    assert CORE_ORACLE_SOURCES <= resolver_sources()


def test_lending_import_registers_exactly_core_sources():
    # In a fresh interpreter, importing only the shared lending package must
    # register exactly the core sources and NOT pull in / register any
    # protocol-specific source (e.g. Danogo). This is the isolation guarantee.
    code = (
        "import charli3_dendrite.lending; "
        "from charli3_dendrite.lending.oracles.models import "
        "resolver_sources, CORE_ORACLE_SOURCES; "
        "got = resolver_sources(); "
        "assert got == CORE_ORACLE_SOURCES, sorted(s.value for s in got)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_public_exports_present():
    for name in lending.__all__:
        assert hasattr(lending, name)


def test_lending_classes_are_not_dex_classes():
    # Ensures lending bases never enter the DEX discovery matrix.
    assert not issubclass(AbstractLendingPoolState, AbstractPairState)
    assert not issubclass(AbstractLoanState, AbstractPairState)
    assert AbstractLendingPoolState not in AbstractPairState.__subclasses__()
    assert AbstractLoanState not in AbstractPairState.__subclasses__()


def test_importing_top_package_does_not_import_lending():
    # Run in a fresh interpreter so prior test imports of lending don't taint
    # sys.modules. Importing the top package must not register resolvers as a
    # side effect of normal DEX usage.
    code = (
        "import charli3_dendrite, sys; "
        "assert 'charli3_dendrite.lending' not in sys.modules, "
        "'top-level import must not pull in lending'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
