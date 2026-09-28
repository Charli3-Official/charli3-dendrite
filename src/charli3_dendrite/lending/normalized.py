"""Protocol-agnostic views of lending markets, positions and borrow requests.

Each protocol converts its own UTxO states into these types, so markets can be listed,
compared and stored in one shape. They are plain frozen dataclasses: a view rebuilt
from :meth:`to_dict` output computes debt, health and cost without the original UTxO.

A value a protocol does not track is ``None``, never ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from math import ceil
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal

from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.rates import MS_PER_HOUR
from charli3_dendrite.lending.rates import RateModel
from charli3_dendrite.lending.rates import rate_model_from_dict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from charli3_dendrite.lending.oracles.models import PriceMap

_POLICY_HEX_LEN = 56


class LendingParseError(ValueError):
    """A UTxO a protocol recognizes but cannot convert into a common view."""

    def __init__(self, reason: str) -> None:
        """Keep ``reason`` for callers that record why a UTxO was not converted."""
        super().__init__(reason)
        self.reason = reason


class PartyKind(str, Enum):
    """How a party is identified."""

    KEY = "key"  # a payment key hash signs
    SCRIPT = "script"  # a script authorizes
    TOKEN = "token"  # whoever holds this unit


class DefaultRule(str, Enum):
    """What happens to a loan's collateral when the loan defaults."""

    PRICE_LIQUIDATION = "price_liquidation"
    FULL_COLLATERAL_CLAIM = "full_collateral_claim"
    DUTCH_AUCTION = "dutch_auction"


def fraction_to_dict(value: Fraction | None) -> dict[str, int] | None:
    """``{"num", "den"}`` of a fraction, or None."""
    if value is None:
        return None
    return {"num": value.numerator, "den": value.denominator}


def fraction_from_dict(data: Mapping[str, int] | None) -> Fraction | None:
    """The fraction of :func:`fraction_to_dict` output, or None."""
    if data is None:
        return None
    return Fraction(int(data["num"]), int(data["den"]))


@dataclass(frozen=True)
class Party:
    """Who someone is: a key hash, a script hash, or the holder of a token."""

    kind: PartyKind
    identifier: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "kind": self.kind.value,
            "identifier": self.identifier,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Party:
        """Rebuild from :meth:`to_dict`."""
        return cls(PartyKind(data["kind"]), data["identifier"], data.get("detail"))


@dataclass(frozen=True)
class CollateralHolding:
    """An amount of one collateral unit."""

    unit: str
    amount: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {"unit": self.unit, "amount": self.amount}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CollateralHolding:
        """Rebuild from :meth:`to_dict`."""
        return cls(data["unit"], int(data["amount"]))


@dataclass(frozen=True)
class Origin:
    """The market or request a position was opened from."""

    kind: Literal["market", "request"]
    identifier: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {"kind": self.kind, "identifier": self.identifier}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Origin:
        """Rebuild from :meth:`to_dict`."""
        return cls(data["kind"], data["identifier"])


@dataclass(frozen=True)
class CollateralTerms:
    """How much can be borrowed against one collateral, and when it is liquidated.

    Exactly one borrowing limit is set: ``max_ltv`` for an oracle-priced market (borrow
    at most the collateral's value times ``max_ltv``) or ``fixed_ratio`` for one without
    an oracle (collateral units required per unit borrowed). ``liquidation_penalty`` is
    a share of the outstanding debt the borrower forfeits; ``liquidation_discount`` is
    the discount at which a liquidator takes collateral.
    """

    unit: str
    policy_wide: bool
    max_ltv: Fraction | None
    fixed_ratio: Fraction | None
    liquidation_ltv: Fraction | None
    liquidation_penalty: Fraction | None
    liquidation_discount: Fraction | None
    equity_to_borrower: bool
    # The feed is a pydantic model, which does not hash; equal terms share it anyway.
    price_source: OracleRef | None = field(hash=False)

    def __post_init__(self) -> None:
        """Refuse terms with no borrowing limit, or with both."""
        if (self.max_ltv is None) == (self.fixed_ratio is None):
            raise ValueError("exactly one of max_ltv and fixed_ratio must be set")

    def accepts(self, unit: str) -> bool:
        """Whether ``unit`` is this collateral (or of its policy, if policy-wide)."""
        if self.policy_wide:
            return unit[:_POLICY_HEX_LEN] == self.unit and unit != "lovelace"
        return unit == self.unit

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "unit": self.unit,
            "policy_wide": self.policy_wide,
            "max_ltv": fraction_to_dict(self.max_ltv),
            "fixed_ratio": fraction_to_dict(self.fixed_ratio),
            "liquidation_ltv": fraction_to_dict(self.liquidation_ltv),
            "liquidation_penalty": fraction_to_dict(self.liquidation_penalty),
            "liquidation_discount": fraction_to_dict(self.liquidation_discount),
            "equity_to_borrower": self.equity_to_borrower,
            "price_source": (
                None
                if self.price_source is None
                else self.price_source.model_dump(mode="json")
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CollateralTerms:
        """Rebuild from :meth:`to_dict`."""
        source = data.get("price_source")
        return cls(
            unit=data["unit"],
            policy_wide=bool(data["policy_wide"]),
            max_ltv=fraction_from_dict(data.get("max_ltv")),
            fixed_ratio=fraction_from_dict(data.get("fixed_ratio")),
            liquidation_ltv=fraction_from_dict(data.get("liquidation_ltv")),
            liquidation_penalty=fraction_from_dict(data.get("liquidation_penalty")),
            liquidation_discount=fraction_from_dict(data.get("liquidation_discount")),
            equity_to_borrower=bool(data["equity_to_borrower"]),
            price_source=None if source is None else OracleRef.model_validate(source),
        )


def _price_in(prices: PriceMap | None, unit: str, quote: str) -> Fraction | None:
    """``unit`` priced in ``quote`` units, or None when missing or not positive."""
    if prices is None:
        return None
    price = prices.get(unit, quote=quote)
    if price is None or price.num <= 0 or price.denom <= 0:
        return None
    return Fraction(price.num, price.denom)


@dataclass(frozen=True)
class LendingMarket:
    """Somewhere to borrow ``borrow_unit``: a lender's pool or a pooled market."""

    protocol: str
    market_id: str
    borrow_unit: str
    lender: Party | None
    available_liquidity: int
    rate_model: RateModel
    supply_rate: Fraction | None
    receipt_unit: str | None
    utilization: Fraction | None
    collateral: tuple[CollateralTerms, ...]
    on_default: DefaultRule
    permissioned: bool

    @property
    def borrow_rate(self) -> Fraction:
        """The rate model's annualized headline rate."""
        return self.rate_model.headline_rate()

    def terms_for(self, collateral_unit: str) -> CollateralTerms | None:
        """Terms for ``collateral_unit``: an exact match, else a policy-wide one."""
        for terms in self.collateral:
            if not terms.policy_wide and terms.accepts(collateral_unit):
                return terms
        for terms in self.collateral:
            if terms.policy_wide and terms.accepts(collateral_unit):
                return terms
        return None

    def required_collateral(
        self,
        collateral_unit: str,
        amount: int,
        prices: PriceMap | None = None,
    ) -> int | None:
        """Least ``collateral_unit`` that backs borrowing ``amount``.

        None when the collateral is not accepted, or when an oracle-priced market has
        no usable price for it in the borrow unit.
        """
        terms = self.terms_for(collateral_unit)
        if terms is None:
            return None
        if terms.fixed_ratio is not None:
            return ceil(amount * terms.fixed_ratio)
        price = _price_in(prices, collateral_unit, self.borrow_unit)
        if price is None or terms.max_ltv is None:
            return None
        return ceil(Fraction(amount) / (terms.max_ltv * price))

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "protocol": self.protocol,
            "market_id": self.market_id,
            "borrow_unit": self.borrow_unit,
            "lender": None if self.lender is None else self.lender.to_dict(),
            "available_liquidity": self.available_liquidity,
            "rate_model": self.rate_model.to_dict(),
            "supply_rate": fraction_to_dict(self.supply_rate),
            "receipt_unit": self.receipt_unit,
            "utilization": fraction_to_dict(self.utilization),
            "collateral": [terms.to_dict() for terms in self.collateral],
            "on_default": self.on_default.value,
            "permissioned": self.permissioned,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LendingMarket:
        """Rebuild from :meth:`to_dict`."""
        lender = data.get("lender")
        return cls(
            protocol=data["protocol"],
            market_id=data["market_id"],
            borrow_unit=data["borrow_unit"],
            lender=None if lender is None else Party.from_dict(lender),
            available_liquidity=int(data["available_liquidity"]),
            rate_model=rate_model_from_dict(data["rate_model"]),
            supply_rate=fraction_from_dict(data.get("supply_rate")),
            receipt_unit=data.get("receipt_unit"),
            utilization=fraction_from_dict(data.get("utilization")),
            collateral=tuple(CollateralTerms.from_dict(t) for t in data["collateral"]),
            on_default=DefaultRule(data["on_default"]),
            permissioned=bool(data["permissioned"]),
        )


@dataclass(frozen=True)
class LendingPosition:
    """An open loan.

    ``principal`` is the principal the loan currently states. Debt, health and
    lateness are evaluated at a caller-supplied time through the loan's rate model.
    """

    protocol: str
    position_id: str
    origin: Origin | None
    borrower: Party
    lender: Party | None
    borrow_unit: str
    principal: int
    installments_paid: int
    collateral: tuple[CollateralHolding, ...]
    rate_model: RateModel
    liquidation_ltv: Fraction | None
    on_default: DefaultRule
    opened_ms: int

    @property
    def next_payment_due_ms(self) -> int | None:
        """When the next installment is due (before any grace window); None if none."""
        period = self.rate_model.installment_period_hours()
        if period is None:
            return None
        hours = self.rate_model.grace_hours() + (self.installments_paid + 1) * period
        return self.opened_ms + hours * MS_PER_HOUR

    @property
    def expires_ms(self) -> int | None:
        """End of the loan term; None when the loan is open-ended."""
        term = self.rate_model.term_hours()
        if term is None:
            return None
        return self.opened_ms + term * MS_PER_HOUR

    def current_debt(self, now_ms: int) -> int:
        """What the loan owes at ``now_ms``."""
        return self.rate_model.outstanding(
            self.principal,
            opened_ms=self.opened_ms,
            now_ms=now_ms,
            installments_paid=self.installments_paid,
        )

    def interest_accrued(self, now_ms: int) -> int:
        """``current_debt`` minus the stated principal.

        For an installment loan this is the schedule's remaining interest.
        """
        return self.current_debt(now_ms) - self.principal

    def collateral_value(self, prices: PriceMap) -> int:
        """Collateral value in the borrow unit; an unpriced holding counts as 0."""
        total = 0
        for holding in self.collateral:
            price = prices.get(holding.unit, quote=self.borrow_unit)
            if price is None or price.num <= 0 or price.denom <= 0:
                continue
            total += holding.amount * price.num // price.denom
        return total

    def health_factor(self, prices: PriceMap, now_ms: int) -> Decimal:
        """Collateral value times the liquidation LTV over the debt; <= 1 is at risk.

        Infinite for a loan that cannot be liquidated on price, or that owes nothing.
        """
        if self.liquidation_ltv is None:
            return Decimal("Infinity")
        debt = self.current_debt(now_ms)
        if debt <= 0:
            return Decimal("Infinity")
        ltv = self.liquidation_ltv
        return (Decimal(self.collateral_value(prices)) * Decimal(ltv.numerator)) / (
            Decimal(debt) * Decimal(ltv.denominator)
        )

    def is_liquidatable(self, prices: PriceMap, now_ms: int) -> bool:
        """Liquidatable on price: every holding priced and the health factor <= 1."""
        for holding in self.collateral:
            if _price_in(prices, holding.unit, self.borrow_unit) is None:
                return False
        return self.health_factor(prices, now_ms) <= 1

    def is_late(self, now_ms: int) -> bool:
        """Whether an installment is overdue at ``now_ms``."""
        return self.rate_model.is_late(
            opened_ms=self.opened_ms,
            now_ms=now_ms,
            installments_paid=self.installments_paid,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "protocol": self.protocol,
            "position_id": self.position_id,
            "origin": None if self.origin is None else self.origin.to_dict(),
            "borrower": self.borrower.to_dict(),
            "lender": None if self.lender is None else self.lender.to_dict(),
            "borrow_unit": self.borrow_unit,
            "principal": self.principal,
            "installments_paid": self.installments_paid,
            "collateral": [holding.to_dict() for holding in self.collateral],
            "rate_model": self.rate_model.to_dict(),
            "liquidation_ltv": fraction_to_dict(self.liquidation_ltv),
            "on_default": self.on_default.value,
            "opened_ms": self.opened_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LendingPosition:
        """Rebuild from :meth:`to_dict`."""
        origin = data.get("origin")
        lender = data.get("lender")
        return cls(
            protocol=data["protocol"],
            position_id=data["position_id"],
            origin=None if origin is None else Origin.from_dict(origin),
            borrower=Party.from_dict(data["borrower"]),
            lender=None if lender is None else Party.from_dict(lender),
            borrow_unit=data["borrow_unit"],
            principal=int(data["principal"]),
            installments_paid=int(data["installments_paid"]),
            collateral=tuple(
                CollateralHolding.from_dict(h) for h in data["collateral"]
            ),
            rate_model=rate_model_from_dict(data["rate_model"]),
            liquidation_ltv=fraction_from_dict(data.get("liquidation_ltv")),
            on_default=DefaultRule(data["on_default"]),
            opened_ms=int(data["opened_ms"]),
        )


@dataclass(frozen=True)
class BorrowRequest:
    """A borrower's open request: collateral locked, waiting for a lender to fill it.

    ``principal_limit`` sets the amount a lender may give (``fixed_ratio``: collateral
    per unit borrowed; ``max_ltv``: priced at fill) and the liquidation terms the
    lender accepts. ``principal_max`` is the request's cap on a fixed-ratio fill.
    """

    protocol: str
    request_id: str
    borrower: Party
    borrow_unit: str
    collateral: CollateralHolding
    principal_limit: CollateralTerms
    principal_max: int
    rate_model: RateModel
    on_default: DefaultRule
    expires_ms: int
    expiry_penalty: int
    permissioned: bool

    def principal_range(
        self,
        prices: PriceMap | None = None,
    ) -> tuple[int, int] | None:
        """The amounts a lender may give, inclusive.

        A fixed-ratio request takes anything from ``ceil(collateral / fixed_ratio)`` up
        to ``principal_max``. A priced request takes exactly
        ``ceil(collateral * price * max_ltv)`` at the fill's prices, with no cap. None
        when no amount qualifies or a needed price is missing.
        """
        limit = self.principal_limit
        if limit.fixed_ratio is not None:
            low = ceil(Fraction(self.collateral.amount) / limit.fixed_ratio)
            if low > self.principal_max:
                return None
            return low, self.principal_max
        price = _price_in(prices, self.collateral.unit, self.borrow_unit)
        if price is None or limit.max_ltv is None:
            return None
        amount = ceil(self.collateral.amount * price * limit.max_ltv)
        return amount, amount

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "borrower": self.borrower.to_dict(),
            "borrow_unit": self.borrow_unit,
            "collateral": self.collateral.to_dict(),
            "principal_limit": self.principal_limit.to_dict(),
            "principal_max": self.principal_max,
            "rate_model": self.rate_model.to_dict(),
            "on_default": self.on_default.value,
            "expires_ms": self.expires_ms,
            "expiry_penalty": self.expiry_penalty,
            "permissioned": self.permissioned,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BorrowRequest:
        """Rebuild from :meth:`to_dict`."""
        return cls(
            protocol=data["protocol"],
            request_id=data["request_id"],
            borrower=Party.from_dict(data["borrower"]),
            borrow_unit=data["borrow_unit"],
            collateral=CollateralHolding.from_dict(data["collateral"]),
            principal_limit=CollateralTerms.from_dict(data["principal_limit"]),
            principal_max=int(data["principal_max"]),
            rate_model=rate_model_from_dict(data["rate_model"]),
            on_default=DefaultRule(data["on_default"]),
            expires_ms=int(data["expires_ms"]),
            expiry_penalty=int(data["expiry_penalty"]),
            permissioned=bool(data["permissioned"]),
        )
