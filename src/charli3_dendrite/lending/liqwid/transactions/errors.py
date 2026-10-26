"""Typed errors raised while building Liqwid V2 transactions.

Every error subclasses `LiqwidBuildError`, itself a `LendingBuildError`, keeps its
constructor arguments as attributes of the same names and carries a one-line
message. Each pickles and copies back to an equal error.
"""

from __future__ import annotations

from fractions import Fraction
from typing import ClassVar
from typing import Literal

from charli3_dendrite.lending.transactions.errors import LendingBuildError

__all__ = [
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
]


class LiqwidBuildError(LendingBuildError):
    """A Liqwid V2 transaction cannot be built as requested."""

    _fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, message: str, *args: object) -> None:
        """Set the message and keep ``args`` under the names in ``_fields``."""
        super().__init__(message)
        for name, value in zip(self._fields, args):
            setattr(self, name, value)
        self._args = args

    def __reduce__(self) -> tuple[type[LiqwidBuildError], tuple[object, ...]]:
        """Rebuild from the constructor arguments, so pickle and copy round-trip."""
        if not self._fields:
            return type(self), (str(self),)
        return type(self), self._args


class ActionUtxoSpentError(LiqwidBuildError):
    """The chosen action UTxO was spent before the transaction was evaluated."""

    _fields = ("out_ref",)
    out_ref: str

    def __init__(self, out_ref: str) -> None:
        """Name the spent action UTxO by its ``<tx hash>#<index>``."""
        super().__init__(f"action UTxO {out_ref} is already spent", out_ref)


class MarketAwaitingBatchError(LiqwidBuildError):
    """No validity window ends after now and by the market's batch deadline."""

    _fields = ("market", "deadline_ms")
    market: str
    deadline_ms: int

    def __init__(self, market: str, deadline_ms: int) -> None:
        """Name the market and its deadline ``s8 + p[3]`` in POSIX milliseconds."""
        super().__init__(
            f"market {market} awaits its next batch (deadline {deadline_ms} ms)",
            market,
            deadline_ms,
        )


class ForeignReferenceInputError(LiqwidBuildError):
    """A loan flow references an input that is neither a script nor a record."""

    _fields = ("out_ref",)
    out_ref: str

    def __init__(self, out_ref: str) -> None:
        """Name the reference input by its ``<tx hash>#<index>``."""
        super().__init__(
            f"reference input {out_ref} is neither a reference script nor a "
            "Liqwid record",
            out_ref,
        )


class UnhealthyLoanError(LiqwidBuildError):
    """The loan after the transaction would fail the health check."""

    _fields = ("required", "available")
    required: Fraction
    available: Fraction

    def __init__(self, required: Fraction, available: Fraction) -> None:
        """Keep the debt value required and the weighted collateral value allowed."""
        super().__init__(
            f"unhealthy loan: debt value {required} exceeds the allowed "
            f"collateral value {available}",
            required,
            available,
        )


class MinValueError(LiqwidBuildError):
    """An action amount is below the market's minimum value ``p[1]``."""

    _fields = ("amount", "minimum")
    amount: int
    minimum: int

    def __init__(self, amount: int, minimum: int) -> None:
        """Keep the amount and the market minimum, in underlying base units."""
        super().__init__(
            f"amount {amount} is below the market minimum {minimum}",
            amount,
            minimum,
        )


class CapExceededError(LiqwidBuildError):
    """The chosen action UTxO would pass the market's borrow or supply cap."""

    _fields = ("cap",)
    cap: Literal["borrow", "supply"]

    def __init__(self, cap: Literal["borrow", "supply"]) -> None:
        """Name the cap: ``"borrow"`` (``p[32]``) or ``"supply"`` (``p[33]``)."""
        super().__init__(f"the market {cap} cap would be exceeded", cap)


class CollateralCountError(LiqwidBuildError):
    """The loan would hold more distinct collateral assets than ``p[14]``."""

    _fields = ("count", "limit")
    count: int
    limit: int

    def __init__(self, count: int, limit: int) -> None:
        """Keep the collateral count and the market limit."""
        super().__init__(
            f"{count} collateral assets exceed the market limit {limit}",
            count,
            limit,
        )


class InsufficientLiquidityError(LiqwidBuildError):
    """The chosen action UTxO cannot pay out the requested amount."""

    _fields = ("needed", "available")
    needed: int
    available: int

    def __init__(self, needed: int, available: int) -> None:
        """Keep the amount needed and the amount the action UTxO can pay."""
        super().__init__(
            f"needs {needed} but the action UTxO can pay {available}",
            needed,
            available,
        )


class UnsupportedCollateralError(LiqwidBuildError):
    """A collateral unit has no exact CollateralParams record to price it by."""

    _fields = ("unit",)
    unit: str

    def __init__(self, unit: str) -> None:
        """Name the collateral unit."""
        super().__init__(
            f"collateral {unit} has no exact CollateralParams record",
            unit,
        )


class UnsupportedMarketError(LiqwidBuildError):
    """The market's records do not support the requested flow."""

    _fields = ("market", "reason")
    market: str
    reason: str

    def __init__(self, market: str, reason: str) -> None:
        """Name the market and why the flow is unsupported there."""
        super().__init__(f"market {market} is not supported: {reason}", market, reason)


class UnsupportedLoanError(LiqwidBuildError):
    """The loan cannot be modified by these builders."""

    _fields = ("out_ref", "reason")
    out_ref: str
    reason: str

    def __init__(self, out_ref: str, reason: str) -> None:
        """Name the loan UTxO by its ``<tx hash>#<index>`` and the reason."""
        super().__init__(f"loan {out_ref} is not supported: {reason}", out_ref, reason)


class MissingFeedError(LiqwidBuildError):
    """No genuine oracle feed prices an asset the transaction needs."""

    _fields = ("asset",)
    asset: str

    def __init__(self, asset: str) -> None:
        """Name the unpriced asset."""
        super().__init__(f"no oracle feed prices {asset}", asset)


class RepayAmountError(LiqwidBuildError):
    """A partial repay exceeds the settled debt without paying the loan off."""

    _fields = ("repay", "debt", "payoff")
    repay: int
    debt: int
    payoff: int

    def __init__(self, repay: int, debt: int, payoff: int) -> None:
        """Keep the repay, the settled debt ``Ds`` and the payoff that closes."""
        super().__init__(
            f"repay {repay} exceeds the settled debt {debt}: pay at most {debt}, "
            f"which leaves a minimum-interest-only loan, or at least the payoff "
            f"{payoff}, which closes",
            repay,
            debt,
            payoff,
        )
