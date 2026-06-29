import subprocess
import sys

from charli3_dendrite.dexs.core.base import AbstractPairState
from charli3_dendrite.lending.danogo import DanogoLoanState
from charli3_dendrite.lending.danogo import DanogoPoolState
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import resolver_sources


def test_danogo_classes_are_not_dex_states():
    assert not issubclass(DanogoPoolState, AbstractPairState)
    assert not issubclass(DanogoLoanState, AbstractPairState)


def test_aggregator_resolver_registered():
    # Registration happens as a side effect of the top-of-file
    # `from charli3_dendrite.lending.danogo import ...` (which runs danogo/__init__.py).
    assert OracleSource.DANOGO_AGGREGATOR in resolver_sources()


def test_top_package_does_not_import_danogo():
    # Run in a fresh interpreter so prior in-process danogo imports can't taint
    # sys.modules (mirrors the tests/lending/test_package.py style).
    code = (
        "import importlib, sys; importlib.import_module('charli3_dendrite');"
        "assert 'charli3_dendrite.lending.danogo' not in sys.modules"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
