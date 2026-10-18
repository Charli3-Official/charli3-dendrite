"""Importing Liqwid V2 modules: what registers, and what stays unloaded."""

import ast
import subprocess
import sys
from pathlib import Path

import charli3_dendrite.lending.liqwid as liqwid_package


def _run(code: str) -> None:
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_the_package_init_is_docstring_only():
    tree = ast.parse(Path(liqwid_package.__file__).read_text())
    assert len(tree.body) == 1
    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)
    assert isinstance(tree.body[0].value.value, str)


def test_importing_the_datums_registers_nothing_and_loads_no_deployment():
    _run(
        "import sys; "
        "import charli3_dendrite.lending.liqwid.datums; "
        "from charli3_dendrite.lending.oracles.models import "
        "CORE_ORACLE_SOURCES, OracleSource, resolver_sources; "
        "got = resolver_sources(); "
        "assert OracleSource.LIQWID not in got, sorted(s.value for s in got); "
        "assert got == CORE_ORACLE_SOURCES, sorted(s.value for s in got); "
        "assert 'charli3_dendrite.lending.liqwid.deployment' not in sys.modules; "
        "assert 'charli3_dendrite.lending.liqwid.oracles' not in sys.modules; "
        "assert 'charli3_dendrite.lending.liqwid.market' not in sys.modules",
    )


def test_danogo_reaches_the_datums_without_the_liqwid_source():
    _run(
        "import sys; "
        "import charli3_dendrite.lending.danogo.oracles.forward; "
        "from charli3_dendrite.lending.oracles.models import "
        "OracleSource, resolver_sources; "
        "assert 'charli3_dendrite.lending.liqwid.datums' in sys.modules; "
        "assert OracleSource.LIQWID not in resolver_sources(); "
        "assert 'charli3_dendrite.lending.liqwid.deployment' not in sys.modules",
    )


def test_importing_the_market_module_registers_the_liqwid_resolver():
    _run(
        "import charli3_dendrite.lending.liqwid.market; "
        "from charli3_dendrite.lending.oracles.models import "
        "OracleSource, resolver_sources; "
        "assert OracleSource.LIQWID in resolver_sources()",
    )


def test_loading_the_builtins_registers_the_liqwid_resolver():
    _run(
        "import sys; "
        "from charli3_dendrite.lending.markets import available_market_sources; "
        "from charli3_dendrite.lending.oracles.models import "
        "OracleSource, resolver_sources; "
        "assert OracleSource.LIQWID not in resolver_sources(); "
        "assert 'charli3_dendrite.lending.liqwid.market' not in sys.modules; "
        "got = available_market_sources(); "
        "assert got == ['fluidtokensv4', 'liqwidv2'], got; "
        "assert OracleSource.LIQWID in resolver_sources()",
    )


def test_liqwid_modules_load_no_other_protocol_package():
    _run(
        "import sys; "
        "import charli3_dendrite.lending.liqwid.market; "
        "import charli3_dendrite.lending.liqwid.lifecycle; "
        "loaded = sorted(m for m in sys.modules "
        "if m.startswith(('charli3_dendrite.lending.fluidtokens', "
        "'charli3_dendrite.lending.danogo'))); "
        "assert not loaded, loaded",
    )
