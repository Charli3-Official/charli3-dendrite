"""FluidTokens V4 rate models: one per repayment mode.

Each carries the loan terms' integers as the datum stores them (``interest_rate`` over
10,000; hours for every period; ``apy_coef`` over 1,000,000) and computes with the
contract-exact maths in :mod:`charli3_dendrite.lending.fluidtokens.math`, which V4
inherits unchanged.

What ``interest_rate`` means depends on the mode: for a perpetual loan it is the annual
rate at borrowing; for the installment modes it is a total for the whole term, so their
headline rate annualizes it over the term.
"""

from __future__ import annotations

import dataclasses
from abc import abstractmethod
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar

from charli3_dendrite.lending.fluidtokens.math import amortization_installment
from charli3_dendrite.lending.fluidtokens.math import installments_pi_amount
from charli3_dendrite.lending.fluidtokens.math import is_repayment_late
from charli3_dendrite.lending.fluidtokens.math import perpetual_debt
from charli3_dendrite.lending.fluidtokens.math import perpetual_outstanding_debt
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.rates import HOURS_PER_YEAR
from charli3_dendrite.lending.rates import RateKind
from charli3_dendrite.lending.rates import RateModel

if TYPE_CHECKING:
    from collections.abc import Mapping

PROTOCOL_NAME = "FluidTokensV4"

_RATE_BASIS = 10_000


@dataclass(frozen=True)
class _FluidRate(RateModel):
    """Terms every FluidTokens repayment mode carries."""

    interest_rate: int
    total_installments: int
    installment_period: int
    initial_grace_period: int
    repayment_time_window: int
    penalty_fee_for_late_repayment: int

    protocol: ClassVar[str] = PROTOCOL_NAME
    _PERPETUAL: ClassVar[bool] = False

    def grace_hours(self) -> int:
        """The initial grace period, in hours."""
        return self.initial_grace_period

    def is_late(self, *, opened_ms: int, now_ms: int, installments_paid: int) -> bool:
        """Past the next installment's repayment window (the contract's rule)."""
        return is_repayment_late(
            is_perpetual=self._PERPETUAL,
            now_ms=now_ms,
            lend_date_ms=opened_ms,
            initial_grace_period=self.initial_grace_period,
            repaid_installments=installments_paid,
            installment_period=self.installment_period,
            repayment_time_window=self.repayment_time_window,
        )

    def params(self) -> dict[str, int]:
        """The datum integers, by field name."""
        return dataclasses.asdict(self)

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> RateModel:
        """Rebuild from :meth:`params`."""
        names = [field.name for field in dataclasses.fields(cls)]
        return cls(**{name: int(params[name]) for name in names})


@dataclass(frozen=True)
class FluidPerpetualRate(_FluidRate):
    """``PerpetualLoan``: open-ended, the rate grows linearly with time held.

    Interest after ``H`` hours is ``principal * (c*H + m*H^2) / 8760`` with
    ``c = interest_rate/10000`` and ``m = apy_coef/1000000``.
    """

    apy_coef: int

    kind: ClassVar[RateKind] = RateKind.PERPETUAL
    _PERPETUAL: ClassVar[bool] = True

    def headline_rate(self) -> Fraction:
        """The annual rate at borrowing, ``c``; the growth ``m`` is in the params."""
        return Fraction(self.interest_rate, _RATE_BASIS)

    def interest_for(self, amount: int, duration_ms: int) -> int:
        """Interest on ``amount`` held ``duration_ms`` with no installment repaid."""
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        debt = perpetual_debt(
            principal=amount,
            interest_rate=self.interest_rate,
            apy_coef=self.apy_coef,
            elapsed_ms=duration_ms,
            installment_period=self.installment_period,
            initial_grace_period=self.initial_grace_period,
        )
        return debt - amount

    def outstanding(
        self,
        principal: int,
        *,
        opened_ms: int,
        now_ms: int,
        installments_paid: int,
    ) -> int:
        """The contract's remaining debt; the principal before the lend date."""
        return perpetual_outstanding_debt(
            principal=principal,
            interest_rate=self.interest_rate,
            apy_coef=self.apy_coef,
            lend_date_ms=opened_ms,
            now_ms=now_ms,
            repaid_installments=installments_paid,
            installment_period=self.installment_period,
            initial_grace_period=self.initial_grace_period,
        )

    def term_hours(self) -> int | None:
        """None: a perpetual loan has no end."""
        return None

    def installment_period_hours(self) -> int | None:
        """The interest-installment period, when the loan has one."""
        return self.installment_period if self.installment_period > 0 else None


@dataclass(frozen=True)
class _FluidTermRate(_FluidRate):
    """A mode repaid over ``total_installments`` installments of a fixed schedule."""

    def __post_init__(self) -> None:
        """Refuse a schedule the contract cannot price."""
        if self.total_installments <= 0:
            raise LendingParseError("an installment loan needs installments")
        if (
            self.initial_grace_period
            + self.total_installments * self.installment_period
            <= 0
        ):
            raise LendingParseError("an installment loan needs a term longer than 0")

    @abstractmethod
    def installment(self, principal: int) -> int:
        """One installment of a loan of ``principal``, paid on time."""

    def interest_for(self, amount: int, duration_ms: int) -> int:
        """The whole schedule's interest; the schedule sets it, not ``duration_ms``."""
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        return self.total_installments * self.installment(amount) - amount

    def outstanding(
        self,
        principal: int,
        *,
        opened_ms: int,  # - the schedule, not time, sets the amount
        now_ms: int,
        installments_paid: int,
    ) -> int:
        """The installments still to pay."""
        remaining = max(self.total_installments - installments_paid, 0)
        return remaining * self.installment(principal)

    def term_hours(self) -> int | None:
        """The grace period plus every installment period."""
        return (
            self.initial_grace_period
            + self.total_installments * self.installment_period
        )

    def installment_period_hours(self) -> int | None:
        """Hours between installments."""
        return self.installment_period

    def _annualized(self, term_total: Fraction) -> Fraction:
        """A term's total rate spread over a year."""
        term = self.term_hours()
        assert term is not None  # noqa: S101 - a term mode always has a term
        return term_total * HOURS_PER_YEAR / term


@dataclass(frozen=True)
class FluidFlatTermRate(_FluidTermRate):
    """``PrincipalAndInterestOnInstallments``: ``principal * r`` of interest, in total.

    The total is split evenly over the installments with the principal.
    """

    kind: ClassVar[RateKind] = RateKind.FLAT_TERM

    def installment(self, principal: int) -> int:
        """``ceil((principal + principal * r) / n)``."""
        return installments_pi_amount(
            principal=principal,
            interest_rate=self.interest_rate,
            total_installments=self.total_installments,
        )

    def headline_rate(self) -> Fraction:
        """The term's total rate ``r``, annualized over the term."""
        return self._annualized(Fraction(self.interest_rate, _RATE_BASIS))


@dataclass(frozen=True)
class FluidAmortizedRate(_FluidTermRate):
    """``InterestOnRemainingPrincipal``: an annuity at ``r / n`` per installment."""

    kind: ClassVar[RateKind] = RateKind.AMORTIZED

    def installment(self, principal: int) -> int:
        """The contract's annuity installment."""
        return amortization_installment(
            principal=principal,
            interest_rate=self.interest_rate,
            total_installments=self.total_installments,
        )

    def headline_rate(self) -> Fraction:
        """The annuity's total interest per unit borrowed, annualized over the term."""
        n = self.total_installments
        rate = Fraction(self.interest_rate, _RATE_BASIS) / n
        if rate == 0:
            return Fraction(0)
        growth = (1 + rate) ** n
        total = n * rate * growth / (growth - 1) - 1
        return self._annualized(total)


# Registered with :mod:`charli3_dendrite.lending.rates` on its first lookup.
RATE_MODELS: tuple[type[RateModel], ...] = (
    FluidPerpetualRate,
    FluidFlatTermRate,
    FluidAmortizedRate,
)
