"""Liqwid V2 rate model: one pooled variable rate, bound to a market's state.

A Liqwid loan stores its principal, its capitalised interest, the minimum interest it
still owes and the market's cumulative interest index when it was last updated. It
owes ``(principal + interest) * market_index / loan_index``, rounded up, and never less
than ``principal + min_interest``. The market index moves only when the market accrues
interest at a batch, so the debt depends on the market state the model is bound to,
never on the clock.

The rate is the per-period borrow rate the market state stores, over the market's
interest period; the headline rate is that rate compounded over a year.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from decimal import Decimal
from decimal import localcontext
from fractions import Fraction
from typing import ClassVar
from typing import Final

from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.math import APY_PRECISION
from charli3_dendrite.lending.liqwid.math import apy_fraction
from charli3_dendrite.lending.liqwid.math import borrow_apy
from charli3_dendrite.lending.liqwid.math import liquidation_debt as unfloored_debt
from charli3_dendrite.lending.liqwid.math import min_interest_at_borrow
from charli3_dendrite.lending.liqwid.math import payoff
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.rates import DataclassIntParams
from charli3_dendrite.lending.rates import RateKind
from charli3_dendrite.lending.rates import RateModel

PROTOCOL_NAME: Final[str] = "LiqwidV2"


@dataclass(frozen=True)
class LiqwidVariableRate(DataclassIntParams):
    """A Liqwid V2 borrow: the market's variable rate and the loan's index terms.

    ``rate_num / rate_den`` is the borrow rate per ``period_ms``, and ``market_index``
    the cumulative index, of the market state this model is bound to. ``loan_index``,
    ``interest`` and ``min_interest`` are the loan's datum fields; a market's own model
    leaves them 0. ``min_rate_num / min_rate_den`` is the market's minimum interest
    rate, charged on the principal of every new borrow.
    """

    rate_num: int
    rate_den: int
    period_ms: int
    market_index: int
    loan_index: int = 0
    interest: int = 0
    min_interest: int = 0
    min_rate_num: int = 1
    min_rate_den: int = 1250

    protocol: ClassVar[str] = PROTOCOL_NAME
    kind: ClassVar[RateKind] = RateKind.VARIABLE

    def __post_init__(self) -> None:
        """Refuse terms the debt and rate formulas cannot evaluate."""
        if self.rate_den <= 0 or self.min_rate_den <= 0:
            raise LendingParseError("a Liqwid rate needs a positive denominator")
        if self.period_ms <= 0:
            raise LendingParseError("a Liqwid interest period must be longer than 0")
        if self.rate_num < 0 or self.min_rate_num < 0:
            raise LendingParseError("a Liqwid rate must not be negative")
        if self.market_index < 0 or self.loan_index < 0:
            raise LendingParseError("a Liqwid interest index must not be negative")

    def _loan(self, principal: int) -> LoanDatum:
        """The loan datum these terms describe, for ``principal``."""
        return LoanDatum(
            owner="",
            principal=principal,
            interest=self.interest,
            min_interest=self.min_interest,
            index=self.loan_index,
        )

    def headline_rate(self) -> Fraction:
        """The borrow APY: the per-period rate compounded over a year."""
        rate = Fraction(self.rate_num, self.rate_den)
        return apy_fraction(borrow_apy(rate, self.period_ms))

    def interest_for(self, amount: int, duration_ms: int) -> int:
        """Interest on ``amount`` borrowed now and held ``duration_ms`` at today's rate.

        A projection for ranking offers: the rate moves with utilization at every
        batch. Never below the minimum interest a borrow of ``amount`` is charged.
        """
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        floor = min_interest_at_borrow(
            amount,
            Fraction(self.min_rate_num, self.min_rate_den),
        )
        if amount == 0 or duration_ms == 0 or self.rate_num == 0:
            return floor
        with localcontext() as ctx:
            ctx.prec = APY_PRECISION
            rate = Decimal(self.rate_num) / Decimal(self.rate_den)
            periods = Decimal(duration_ms) / Decimal(self.period_ms)
            growth = ((1 + rate).ln() * periods).exp()
            projected = math.ceil(Decimal(amount) * (growth - 1))
        return max(projected, floor)

    def outstanding(
        self,
        principal: int,
        *,
        opened_ms: int,
        now_ms: int,
        installments_paid: int,
    ) -> int:
        """The floored payoff at the bound market index; the principal at index 0."""
        if self.loan_index == 0:
            return principal
        return payoff(self._loan(principal), self.market_index)

    def liquidation_debt(
        self,
        principal: int,
        *,
        opened_ms: int,
        now_ms: int,
        installments_paid: int,
    ) -> int:
        """The unfloored debt health is measured against; the principal at index 0."""
        if self.loan_index == 0:
            return principal
        return unfloored_debt(self._loan(principal), self.market_index)

    def remaining_principal(self, principal: int, installments_paid: int) -> int:
        """The whole principal: repayments settle interest and principal together."""
        return principal

    def is_late(self, *, opened_ms: int, now_ms: int, installments_paid: int) -> bool:
        """Never: a Liqwid loan has no repayment schedule."""
        return False

    def term_hours(self) -> int | None:
        """None: a Liqwid loan is open-ended."""
        return None

    def installment_period_hours(self) -> int | None:
        """None: a Liqwid loan has no installments."""
        return None

    def grace_hours(self) -> int:
        """0: no grace period."""
        return 0

    def rebind(self, market_model: RateModel) -> LiqwidVariableRate:
        """These loan terms at the rate, period and index of ``market_model``.

        Raises:
            TypeError: if ``market_model`` is not a Liqwid variable rate.
        """
        if not isinstance(market_model, LiqwidVariableRate):
            raise TypeError(
                "rebind needs a LiqwidVariableRate market model, "
                f"got {type(market_model).__name__}",
            )
        return dataclasses.replace(
            self,
            rate_num=market_model.rate_num,
            rate_den=market_model.rate_den,
            period_ms=market_model.period_ms,
            market_index=market_model.market_index,
        )


# The models this package registers with :mod:`charli3_dendrite.lending.rates`.
RATE_MODELS: tuple[type[RateModel], ...] = (LiqwidVariableRate,)
