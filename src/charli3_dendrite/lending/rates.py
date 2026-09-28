"""Protocol-agnostic interest-rate models for lending markets, positions and requests.

A rate model states what borrowing costs under one protocol's loan terms. Every model
gives an annualized headline rate, for sorting and storage, and the exact interest a
borrow owes, computed with the protocol's own maths.

Concrete models live in each protocol's package and register here by ``(protocol,
kind)``, so a stored ``{"protocol", "kind", "params"}`` record rebuilds its model.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar

if TYPE_CHECKING:
    from collections.abc import Mapping
    from fractions import Fraction

HOURS_PER_YEAR = 8760
MS_PER_HOUR = 3_600_000


class RateKind(str, Enum):
    """How a borrow's interest accrues."""

    PERPETUAL = "perpetual"  # open-ended; interest grows with time held
    FLAT_TERM = "flat_term"  # a fixed total for the term, paid in installments
    AMORTIZED = "amortized"  # an annuity over the installments
    VARIABLE = "variable"  # a pooled rate that moves with the market


class RateModel(ABC):
    """The cost of borrowing under one protocol's terms."""

    protocol: ClassVar[str]
    kind: ClassVar[RateKind]

    @abstractmethod
    def headline_rate(self) -> Fraction:
        """Annualized rate: the rate at borrowing, or a term total over a year."""

    @abstractmethod
    def interest_for(self, amount: int, duration_ms: int) -> int:
        """Interest ``amount`` owes over ``duration_ms``, repaid on schedule."""

    @abstractmethod
    def outstanding(
        self,
        principal: int,
        *,
        opened_ms: int,
        now_ms: int,
        installments_paid: int,
    ) -> int:
        """What a loan of ``principal`` opened at ``opened_ms`` owes at ``now_ms``."""

    @abstractmethod
    def is_late(self, *, opened_ms: int, now_ms: int, installments_paid: int) -> bool:
        """Whether an installment is overdue at ``now_ms``."""

    @abstractmethod
    def term_hours(self) -> int | None:
        """Length of the loan term in hours; None when open-ended."""

    @abstractmethod
    def installment_period_hours(self) -> int | None:
        """Hours between installments; None when there are none."""

    @abstractmethod
    def grace_hours(self) -> int:
        """Hours before the first installment period starts."""

    @abstractmethod
    def params(self) -> dict[str, int]:
        """The protocol parameters that rebuild this model with :meth:`from_params`."""

    @classmethod
    @abstractmethod
    def from_params(cls, params: Mapping[str, Any]) -> RateModel:
        """Rebuild the model from :meth:`params`."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe record that :func:`rate_model_from_dict` rebuilds."""
        return {
            "protocol": self.protocol,
            "kind": self.kind.value,
            "params": self.params(),
        }


_REGISTRY: dict[tuple[str, RateKind], type[RateModel]] = {}


def register_rate_model(cls: type[RateModel]) -> type[RateModel]:
    """Register (or replace) a rate model under its ``(protocol, kind)``."""
    _REGISTRY[(cls.protocol, cls.kind)] = cls
    return cls


def rate_model_from_dict(data: Mapping[str, Any]) -> RateModel:
    """Rebuild a rate model from :meth:`RateModel.to_dict`.

    Raises:
        KeyError: if no model is registered for the record's protocol and kind.
    """
    key = (str(data["protocol"]), RateKind(data["kind"]))
    try:
        cls = _REGISTRY[key]
    except KeyError:
        known = ", ".join(f"{p}/{k.value}" for p, k in sorted(_REGISTRY)) or "(none)"
        raise KeyError(
            f"no rate model registered for {key[0]}/{key[1].value}; known: {known}",
        ) from None
    return cls.from_params(data["params"])
