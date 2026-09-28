"""Protocol-free builders for the common lending views, shared by the view tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any
from typing import ClassVar

from charli3_dendrite.lending.normalized import BorrowRequest
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import CollateralTerms
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.rates import HOURS_PER_YEAR
from charli3_dendrite.lending.rates import MS_PER_HOUR
from charli3_dendrite.lending.rates import RateKind
from charli3_dendrite.lending.rates import RateModel
from charli3_dendrite.lending.rates import register_rate_model

SNEK = "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f534e454b"
MIN = "29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c64d494e"
NFT_POLICY = "b" * 56
_YEAR_MS = HOURS_PER_YEAR * MS_PER_HOUR


@dataclass(frozen=True)
class SimpleRate(RateModel):
    """Open-ended simple interest at ``rate`` per 10,000 a year."""

    rate: int

    protocol: ClassVar[str] = "Test"
    kind: ClassVar[RateKind] = RateKind.VARIABLE

    def headline_rate(self) -> Fraction:
        return Fraction(self.rate, 10_000)

    def interest_for(self, amount: int, duration_ms: int) -> int:
        return amount * self.rate * duration_ms // (10_000 * _YEAR_MS)

    def outstanding(
        self,
        principal: int,
        *,
        opened_ms: int,
        now_ms: int,
        installments_paid: int,
    ) -> int:
        return principal + self.interest_for(principal, max(now_ms - opened_ms, 0))

    def is_late(self, *, opened_ms: int, now_ms: int, installments_paid: int) -> bool:
        return False

    def term_hours(self) -> int | None:
        return None

    def installment_period_hours(self) -> int | None:
        return None

    def grace_hours(self) -> int:
        return 0

    def params(self) -> dict[str, int]:
        return {"rate": self.rate}

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> RateModel:
        return cls(int(params["rate"]))


register_rate_model(SimpleRate)


def terms(unit: str = SNEK, **overrides: Any) -> CollateralTerms:
    """Oracle-priced terms: borrow up to 2/3, liquidate at 4/5."""
    values: dict[str, Any] = {
        "unit": unit,
        "policy_wide": False,
        "max_ltv": Fraction(2, 3),
        "fixed_ratio": None,
        "liquidation_ltv": Fraction(4, 5),
        "liquidation_penalty": Fraction(1, 10),
        "liquidation_discount": None,
        "equity_to_borrower": True,
        "price_source": None,
    }
    values.update(overrides)
    return CollateralTerms(**values)


def market(market_id: str = "m1", **overrides: Any) -> LendingMarket:
    """A single-lender ADA market accepting SNEK."""
    values: dict[str, Any] = {
        "protocol": "Test",
        "market_id": market_id,
        "borrow_unit": "lovelace",
        "lender": Party(PartyKind.KEY, "aa" * 28),
        "available_liquidity": 1_000_000_000,
        "rate_model": SimpleRate(500),
        "supply_rate": None,
        "receipt_unit": None,
        "utilization": None,
        "collateral": (terms(),),
        "on_default": DefaultRule.PRICE_LIQUIDATION,
        "permissioned": False,
    }
    values.update(overrides)
    return LendingMarket(**values)


def request(request_id: str = "r1", **overrides: Any) -> BorrowRequest:
    """A fixed-ratio request: 875 SNEK per lovelace, capped at 1,000 ADA."""
    values: dict[str, Any] = {
        "protocol": "Test",
        "request_id": request_id,
        "borrower": Party(PartyKind.KEY, "bb" * 28),
        "borrow_unit": "lovelace",
        "collateral": CollateralHolding(SNEK, 875_000_000),
        "principal_limit": terms(max_ltv=None, fixed_ratio=Fraction(875)),
        "principal_max": 1_000_000_000,
        "rate_model": SimpleRate(700),
        "on_default": DefaultRule.PRICE_LIQUIDATION,
        "expires_ms": 1_800_000_000_000,
        "expiry_penalty": 2_000_000,
        "permissioned": False,
    }
    values.update(overrides)
    return BorrowRequest(**values)


def prices(*entries: tuple[str, str, int, int]) -> PriceMap:
    """A price map of ``(token, quote, num, denom)`` entries."""
    book = PriceMap()
    for token, quote, num, denom in entries:
        book.add(
            OraclePrice(
                token=token,
                quote=quote,
                num=num,
                denom=denom,
                source=OracleSource.FLUID_AGGREGATED,
            ),
        )
    return book
