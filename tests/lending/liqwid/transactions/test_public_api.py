"""The public surface of the Liqwid V2 transaction builders.

The package exports the builder, the snapshots and builds, and the primitives a
liquidation composes; the shared seam names stay in their ``lending.transactions``
modules and the protocol records in ``lending.liqwid``.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

import charli3_dendrite.lending.liqwid.transactions as package
from charli3_dendrite.lending.liqwid.transactions import errors
from charli3_dendrite.lending.transactions.base import LendingAction
from charli3_dendrite.lending.transactions.errors import LendingBuildError

_PACKAGE = "charli3_dendrite.lending.liqwid.transactions"

# The package names a liquidation builder composes.
LIQUIDATION_PRIMITIVES = [
    "ActionDeltas",
    "Settlement",
    "settle",
    "repay_from_settlement",
    "apply_repay",
    "ttl_cap_ms",
    "collateral_holdings",
    "add_action_leg",
    "add_loan_input",
    "add_staking_withdrawal",
    "loan_output",
    "health_reference_set",
    "check_reference_hygiene",
    "LiqwidScriptRefs",
    "resolve_script_refs",
    "resolve_action_utxos",
    "choose_action_utxo",
    "resolve_loan",
    "resolve_market_records",
    "resolve_feeds",
]

BUILDERS = [
    "LiqwidTxBuilder",
    "SupplySnapshot",
    "RedeemSnapshot",
    "BorrowSnapshot",
    "IncreaseSnapshot",
    "RepaySnapshot",
    "CollateralSnapshot",
    "SupplyAndBorrowSnapshot",
    "RemoveAndRedeemSnapshot",
    "build_supply",
    "build_redeem",
    "build_borrow",
    "build_increase",
    "build_repay",
    "build_collateral",
    "build_supply_and_borrow",
    "build_remove_and_redeem",
]

# Owner-flow checks: importable from ``terms``, not exported by the package.
OWNER_FLOW_ONLY = [
    "check_limits",
    "check_market_limits",
    "check_action_limits",
    "check_health",
]

# Where the other public primitives live: module -> names.
ELSEWHERE = {
    "charli3_dendrite.lending.transactions.utxos": [
        "Utxo",
        "to_pycardano_utxo",
        "utxo_value",
        "ogmios_entry",
        "utxo_from_dict",
        "utxo_from_info",
        "script_ref_by_hash",
        "script_hash_of",
    ],
    "charli3_dendrite.lending.transactions.common": [
        "plutus_int",
        "add_zero_withdrawal",
        "merge_reference_inputs",
        "ref_index",
        "input_index",
        "reward_address",
        "with_reference_script",
    ],
    "charli3_dendrite.lending.transactions.validity": ["merge_validity"],
    "charli3_dendrite.lending.transactions.errors": [
        "LendingBuildError",
        "OgmiosEvaluationError",
        "ValidityWindowError",
    ],
}
LIQWID_RECORDS = {
    "charli3_dendrite.lending.liqwid.deployment": [
        "StakingRole",
        "staking_roles",
        "ReferenceUtxo",
    ],
    "charli3_dendrite.lending.liqwid.math": [
        "liquidation_debt",
        "liquidation_repay",
        "seized_value",
        "seized_qtokens",
        "health",
        "liquidation_weight",
    ],
    "charli3_dendrite.lending.liqwid.datums": [
        "encode_loan_datum",
        "encode_action_datum",
        "encode_market_state",
        "datum_style",
    ],
    "charli3_dendrite.lending.liqwid.indexing": ["action_selector", "parse_action"],
}

# Each Liqwid error and a test that raises it (``module::function``).
RAISED_BY = {
    "LiqwidBuildError": (
        "tests.lending.liqwid.transactions.test_builder::"
        "test_a_spent_action_utxo_is_named_and_a_retry_excluding_it_picks_another"
    ),
    "ActionUtxoSpentError": (
        "tests.lending.liqwid.transactions.test_builder::"
        "test_a_spent_action_utxo_is_named_and_a_retry_excluding_it_picks_another"
    ),
    "CapExceededError": (
        "tests.lending.liqwid.transactions.test_supply::"
        "test_the_supply_cap_binds_a_supply_but_not_a_redeem"
    ),
    "CollateralCountError": (
        "tests.lending.liqwid.transactions.test_collateral::"
        "test_a_seventh_collateral_on_a_six_collateral_market_is_refused"
    ),
    "ForeignReferenceInputError": (
        "tests.lending.liqwid.transactions.test_legs::"
        "test_hygiene_refuses_anything_else"
    ),
    "InsufficientLiquidityError": (
        "tests.lending.liqwid.transactions.test_supply::"
        "test_a_redeem_beyond_the_action_utxo_is_refused"
    ),
    "MarketAwaitingBatchError": (
        "tests.lending.liqwid.transactions.test_supply::"
        "test_a_live_supply_at_the_batch_deadline_waits_for_the_batch"
    ),
    "MinValueError": (
        "tests.lending.liqwid.transactions.test_supply::"
        "test_a_supply_below_the_market_minimum_is_refused_by_the_build"
    ),
    "MissingFeedError": (
        "tests.lending.liqwid.transactions.test_resolve::"
        "test_a_missing_feed_is_refused"
    ),
    "RepayAmountError": (
        "tests.lending.liqwid.transactions.test_repay::"
        "test_repaying_the_settled_debt_leaves_a_minimum_interest_only_loan"
    ),
    "UnhealthyLoanError": (
        "tests.lending.liqwid.transactions.test_collateral::"
        "test_a_removal_is_held_to_the_exact_health_boundary"
    ),
    "UnsupportedCollateralError": (
        "tests.lending.liqwid.transactions.test_resolve::"
        "test_a_collateral_without_an_exact_record_is_refused"
    ),
    "UnsupportedLoanError": (
        "tests.lending.liqwid.transactions.test_resolve::"
        "test_a_legacy_generation_loan_is_refused"
    ),
    "UnsupportedMarketError": (
        "tests.lending.liqwid.transactions.test_resolve::"
        "test_action_utxos_of_a_market_without_an_action_token_policy_are_refused"
    ),
}


def _run(code: str) -> None:
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_every_exported_name_imports() -> None:
    assert len(set(package.__all__)) == len(package.__all__)
    assert set(package.__all__) == set(package._MODULE_OF)
    for name in package.__all__:
        exec(f"from {_PACKAGE} import {name}", {})  # noqa: S102 - a fixed import
        assert getattr(package, name) is not None


def test_type_checkers_see_every_exported_name_from_its_module() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(package.__file__ or "").read_text())
    (guard,) = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
    ]
    seen = {
        alias.name: node.module
        for node in guard.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert seen == {
        name: f"{_PACKAGE}.{module}" for name, module in package._MODULE_OF.items()
    }


def test_each_name_is_the_owning_module_object() -> None:
    for name in package.__all__:
        value = getattr(package, name)
        owner = importlib.import_module(value.__module__)
        assert getattr(owner, name) is value, name


@pytest.mark.parametrize("name", BUILDERS + LIQUIDATION_PRIMITIVES)
def test_the_package_exports(name: str) -> None:
    assert name in package.__all__


def test_the_package_exports_every_liqwid_error() -> None:
    assert set(errors.__all__) <= set(package.__all__)


@pytest.mark.parametrize("name", OWNER_FLOW_ONLY)
def test_owner_flow_checks_are_not_exported(name: str) -> None:
    from charli3_dendrite.lending.liqwid.transactions import terms

    assert hasattr(terms, name)
    assert name not in package.__all__
    with pytest.raises(AttributeError):
        getattr(package, name)


def test_an_unknown_name_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        package.no_such_name  # noqa: B018


@pytest.mark.parametrize(
    ("module", "names"),
    [*ELSEWHERE.items(), *LIQWID_RECORDS.items()],
)
def test_the_other_primitives_import_from_their_modules(
    module: str,
    names: list[str],
) -> None:
    imported = importlib.import_module(module)
    for name in names:
        assert hasattr(imported, name), f"{module}.{name}"
    exported = getattr(imported, "__all__", None)
    if module in ELSEWHERE:
        assert exported is not None
    if exported is not None:
        assert set(names) <= set(exported)


# The named MarketParams fields and properties a liquidation reads.
MARKET_PARAMS_NAMES = [
    "min_value",
    "max_batch_time_ms",
    "action_count",
    "max_collaterals",
    "borrow_cap",
    "supply_cap",
]


@pytest.mark.parametrize("name", MARKET_PARAMS_NAMES)
def test_market_params_name_their_fields(name: str) -> None:
    import dataclasses

    from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum

    fields = {f.name for f in dataclasses.fields(MarketParamsDatum)}
    member = getattr(MarketParamsDatum, name, None)
    assert name in fields or isinstance(member, property), name


def test_market_params_properties_read_their_fields() -> None:
    from charli3_dendrite.lending.liqwid.deployment import default_deployment
    from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
    from tests.lending.liqwid.transactions.replay import fixture

    ada = default_deployment().market_by_name("Ada")
    params = CapturedTx.of(fixture("supply_ada")).market_records(ada).params_datum
    assert params.min_value == params.raw[1]
    assert params.max_batch_time_ms == params.raw[3]
    assert params.action_count == params.raw[7]


def test_the_action_kind_is_public() -> None:
    from charli3_dendrite.lending.liqwid.indexing import LiqwidKind

    assert LiqwidKind.ACTION.value == "action"


def test_the_package_loads_its_modules_lazily() -> None:
    _run(
        "import sys; "
        f"import {_PACKAGE}; "
        f"import {_PACKAGE}.terms; "
        "bad = sorted(m for m in sys.modules if m in ("
        "'charli3_dendrite.lending.liqwid.oracles', "
        "'charli3_dendrite.lending.liqwid.deployment', "
        f"'{_PACKAGE}.builder', "
        f"'{_PACKAGE}.resolve', "
        f"'{_PACKAGE}.legs')); "
        "assert not bad, bad",
    )


def test_a_name_loads_its_owning_module() -> None:
    _run(
        "import sys; "
        f"from {_PACKAGE} import settle; "
        f"assert '{_PACKAGE}.terms' in sys.modules; "
        f"assert '{_PACKAGE}.builder' not in sys.modules; "
        "assert 'charli3_dendrite.lending.liqwid.oracles' not in sys.modules",
    )


def test_lending_action_has_no_liquidate() -> None:
    assert "LIQUIDATE" not in LendingAction.__members__
    assert "liquidate" not in {a.value for a in LendingAction}


def test_every_liqwid_error_is_a_lending_build_error() -> None:
    for name in errors.__all__:
        error = getattr(errors, name)
        assert issubclass(error, LendingBuildError), name
        assert issubclass(error, errors.LiqwidBuildError), name


def test_every_liqwid_error_is_raised_by_a_test() -> None:
    assert set(RAISED_BY) == set(errors.__all__)
    for name, test_id in RAISED_BY.items():
        module, function = test_id.split("::")
        test = getattr(importlib.import_module(module), function, None)
        assert callable(test), f"{name}: no test {test_id}"


def test_importing_lending_registers_only_the_core_oracle_sources() -> None:
    _run(
        "import charli3_dendrite.lending; "
        "from charli3_dendrite.lending.oracles.models import "
        "resolver_sources, CORE_ORACLE_SOURCES; "
        "got = resolver_sources(); "
        "assert got == CORE_ORACLE_SOURCES, sorted(s.value for s in got)",
    )


def test_importing_the_registry_registers_the_liqwid_read_side() -> None:
    _run(
        "import charli3_dendrite.lending.registry as registry; "
        "from charli3_dendrite.lending.oracles.models import "
        "resolver_sources, OracleSource; "
        "assert 'liqwid' in registry.available_lending_protocols(); "
        "assert OracleSource.LIQWID in resolver_sources()",
    )
