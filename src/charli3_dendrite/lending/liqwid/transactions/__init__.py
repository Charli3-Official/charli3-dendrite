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
module (``terms`` for the arithmetic alone) loads no other; type checkers see them
through the ``TYPE_CHECKING`` imports. The owner-flow limit and health checks stay
in ``terms``; the shared seam names live in their
``charli3_dendrite.lending.transactions`` modules.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
    from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
    from charli3_dendrite.lending.liqwid.transactions.borrow_more import (
        IncreaseSnapshot,
    )
    from charli3_dendrite.lending.liqwid.transactions.borrow_more import build_increase
    from charli3_dendrite.lending.liqwid.transactions.builder import LiqwidTxBuilder
    from charli3_dendrite.lending.liqwid.transactions.collateral import (
        CollateralSnapshot,
    )
    from charli3_dendrite.lending.liqwid.transactions.collateral import build_collateral
    from charli3_dendrite.lending.liqwid.transactions.combos import (
        RemoveAndRedeemSnapshot,
    )
    from charli3_dendrite.lending.liqwid.transactions.combos import (
        SupplyAndBorrowSnapshot,
    )
    from charli3_dendrite.lending.liqwid.transactions.combos import (
        build_remove_and_redeem,
    )
    from charli3_dendrite.lending.liqwid.transactions.combos import (
        build_supply_and_borrow,
    )
    from charli3_dendrite.lending.liqwid.transactions.errors import ActionUtxoSpentError
    from charli3_dendrite.lending.liqwid.transactions.errors import CapExceededError
    from charli3_dendrite.lending.liqwid.transactions.errors import CollateralCountError
    from charli3_dendrite.lending.liqwid.transactions.errors import (
        ForeignReferenceInputError,
    )
    from charli3_dendrite.lending.liqwid.transactions.errors import (
        InsufficientLiquidityError,
    )
    from charli3_dendrite.lending.liqwid.transactions.errors import LiqwidBuildError
    from charli3_dendrite.lending.liqwid.transactions.errors import (
        MarketAwaitingBatchError,
    )
    from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
    from charli3_dendrite.lending.liqwid.transactions.errors import MissingFeedError
    from charli3_dendrite.lending.liqwid.transactions.errors import RepayAmountError
    from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
    from charli3_dendrite.lending.liqwid.transactions.errors import (
        UnsupportedCollateralError,
    )
    from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedLoanError
    from charli3_dendrite.lending.liqwid.transactions.errors import (
        UnsupportedMarketError,
    )
    from charli3_dendrite.lending.liqwid.transactions.legs import add_action_leg
    from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_input
    from charli3_dendrite.lending.liqwid.transactions.legs import add_staking_withdrawal
    from charli3_dendrite.lending.liqwid.transactions.legs import (
        check_reference_hygiene,
    )
    from charli3_dendrite.lending.liqwid.transactions.legs import health_reference_set
    from charli3_dendrite.lending.liqwid.transactions.legs import loan_output
    from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
    from charli3_dendrite.lending.liqwid.transactions.repay import build_repay
    from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
    from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
    from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
    from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
    from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
    from charli3_dendrite.lending.liqwid.transactions.resolve import choose_action_utxo
    from charli3_dendrite.lending.liqwid.transactions.resolve import (
        collateral_params_records,
    )
    from charli3_dendrite.lending.liqwid.transactions.resolve import (
        resolve_action_utxos,
    )
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_feeds
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
    from charli3_dendrite.lending.liqwid.transactions.resolve import (
        resolve_market_records,
    )
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_script_refs
    from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
    from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
    from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
    from charli3_dendrite.lending.liqwid.transactions.supply import build_supply
    from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
    from charli3_dendrite.lending.liqwid.transactions.terms import RepayResult
    from charli3_dendrite.lending.liqwid.transactions.terms import Settlement
    from charli3_dendrite.lending.liqwid.transactions.terms import apply_repay
    from charli3_dendrite.lending.liqwid.transactions.terms import collateral_holdings
    from charli3_dendrite.lending.liqwid.transactions.terms import repay_from_settlement
    from charli3_dendrite.lending.liqwid.transactions.terms import settle
    from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms

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
