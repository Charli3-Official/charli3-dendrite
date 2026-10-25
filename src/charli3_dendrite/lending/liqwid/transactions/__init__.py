"""Liqwid V2 transaction builders on the shared lending transaction seam.

``errors.py`` holds the Liqwid build errors and ``terms.py`` the pure arithmetic
of every Liqwid V2 user flow: loan settlement, the per-action loan records and
action-UTxO deltas, and supply and redeem amounts. ``resolve.py`` reads from the
chain what a transaction spends and references: reference scripts, action UTxOs,
loans, market records and oracle feeds; ``legs.py`` holds the composable pieces
a transaction is built from. ``supply.py``, ``borrow.py``, ``borrow_more.py``,
``repay.py``, ``collateral.py`` and ``combos.py`` hold each flow's snapshot and
build, and ``builder.py`` the :class:`LiqwidTxBuilder` registered as ``"liqwid"``.

The names in ``__all__`` load their module on first access, so importing one
module (``terms`` for the arithmetic alone) loads no other. The owner-flow limit
and health checks stay in ``terms``; the shared seam names live in their
``charli3_dendrite.lending.transactions`` modules.
"""

from __future__ import annotations

import importlib
from typing import Any

# The module, relative to this package, of each name in ``__all__``.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "builder": ("LiqwidTxBuilder",),
    "supply": ("RedeemSnapshot", "SupplySnapshot", "build_redeem", "build_supply"),
    "borrow": ("BorrowSnapshot", "build_borrow"),
    "borrow_more": ("IncreaseSnapshot", "build_increase"),
    "repay": ("RepaySnapshot", "build_repay"),
    "collateral": ("CollateralSnapshot", "build_collateral"),
    "combos": (
        "RemoveAndRedeemSnapshot",
        "SupplyAndBorrowSnapshot",
        "build_remove_and_redeem",
        "build_supply_and_borrow",
    ),
    "terms": (
        "ActionDeltas",
        "RepayResult",
        "Settlement",
        "apply_repay",
        "collateral_holdings",
        "repay_from_settlement",
        "settle",
        "ttl_cap_ms",
    ),
    "legs": (
        "add_action_leg",
        "add_loan_input",
        "add_staking_withdrawal",
        "check_reference_hygiene",
        "health_reference_set",
        "loan_output",
    ),
    "resolve": (
        "ActionUtxo",
        "HealthInputs",
        "LiqwidScriptRefs",
        "LoanRecord",
        "MarketRecords",
        "choose_action_utxo",
        "collateral_params_records",
        "resolve_action_utxos",
        "resolve_feeds",
        "resolve_loan",
        "resolve_market_records",
        "resolve_script_refs",
    ),
    "errors": (
        "ActionUtxoSpentError",
        "CapExceededError",
        "CollateralCountError",
        "ForeignReferenceInputError",
        "InsufficientLiquidityError",
        "LiqwidBuildError",
        "MarketAwaitingBatchError",
        "MinValueError",
        "MissingFeedError",
        "RepayAmountError",
        "UnhealthyLoanError",
        "UnsupportedCollateralError",
        "UnsupportedLoanError",
        "UnsupportedMarketError",
    ),
}
_MODULE_OF = {name: module for module, names in _EXPORTS.items() for name in names}

__all__ = [
    "ActionDeltas",
    "ActionUtxo",
    "ActionUtxoSpentError",
    "BorrowSnapshot",
    "CapExceededError",
    "CollateralCountError",
    "CollateralSnapshot",
    "ForeignReferenceInputError",
    "HealthInputs",
    "IncreaseSnapshot",
    "InsufficientLiquidityError",
    "LiqwidBuildError",
    "LiqwidScriptRefs",
    "LiqwidTxBuilder",
    "LoanRecord",
    "MarketAwaitingBatchError",
    "MarketRecords",
    "MinValueError",
    "MissingFeedError",
    "RedeemSnapshot",
    "RemoveAndRedeemSnapshot",
    "RepayAmountError",
    "RepayResult",
    "RepaySnapshot",
    "Settlement",
    "SupplyAndBorrowSnapshot",
    "SupplySnapshot",
    "UnhealthyLoanError",
    "UnsupportedCollateralError",
    "UnsupportedLoanError",
    "UnsupportedMarketError",
    "add_action_leg",
    "add_loan_input",
    "add_staking_withdrawal",
    "apply_repay",
    "build_borrow",
    "build_collateral",
    "build_increase",
    "build_redeem",
    "build_remove_and_redeem",
    "build_repay",
    "build_supply",
    "build_supply_and_borrow",
    "check_reference_hygiene",
    "choose_action_utxo",
    "collateral_holdings",
    "collateral_params_records",
    "health_reference_set",
    "loan_output",
    "repay_from_settlement",
    "resolve_action_utxos",
    "resolve_feeds",
    "resolve_loan",
    "resolve_market_records",
    "resolve_script_refs",
    "settle",
    "ttl_cap_ms",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401 - any exported object
    """Import the module that owns ``name`` and return its ``name``."""
    module = _MODULE_OF.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """The module's attributes and the lazily loaded exports."""
    return sorted({*globals(), *__all__})
