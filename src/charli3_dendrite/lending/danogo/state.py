"""Danogo read-only pool & loan state."""

from __future__ import annotations

from decimal import Decimal

from pycardano import Address
from pycardano import PlutusData
from pydantic import PrivateAttr

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.lending.base import AbstractLendingPoolState
from charli3_dendrite.lending.base import AbstractLoanState
from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.math import current_interest_index
from charli3_dendrite.lending.danogo.math import current_loan_amount
from charli3_dendrite.lending.danogo.math import total_collateral_val
from charli3_dendrite.lending.danogo.math import total_collateral_val_with_threshold
from charli3_dendrite.lending.danogo.math import util_rate
from charli3_dendrite.lending.math import bps_mul_floor
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap


class DanogoPoolState(AbstractLendingPoolState):
    """Snapshot of one Danogo pool/market."""

    address: str
    _market: DanogoMarket | None = PrivateAttr(default=None)

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "Danogo"

    @classmethod
    def pool_datum_class(cls) -> type[PlutusData]:
        """Pool datum type."""
        return PoolDatum

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool UTxOs are discovered by the loader; selector built there."""
        return PoolSelector(addresses=[], assets=None)

    def attach_market(self, market: DanogoMarket) -> None:
        """Attach the market params this pool references."""
        self._market = market

    @property
    def market(self) -> DanogoMarket:
        """The attached market (raises if not yet attached)."""
        if self._market is None:
            raise ValueError("market not attached")
        return self._market

    @property
    def stake_address(self) -> Address:
        """Script address of the pool UTxO."""
        return Address.decode(self.address)

    @property
    def pool_id(self) -> str:
        """Stable id: pool address + supply token."""
        return f"{self.address}:{self.market.supply_token}"

    @property
    def borrowable_unit(self) -> str:
        """Unit suppliers lend / borrowers borrow."""
        return self.market.supply_token

    @property
    def utilization_ratio(self) -> Decimal:
        """total_borrow / total_supply (0 when supply is 0)."""
        d: PoolDatum = self.pool_datum  # type: ignore[assignment]
        return util_rate(total_borrow=d.total_borrow, total_supply=d.total_supply)

    def oracle_refs(self) -> list[OracleRef]:
        """Pools carry no oracle refs; collateral pricing is driven per-loan."""
        return []

    def max_borrow_for_collateral_value(self, collateral_value_lovelace: int) -> int:
        """Best-effort: apply the strongest accepted collateral threshold."""
        if not self.market.collaterals:
            return 0
        best_threshold = max(self.market.collaterals.values())
        return bps_mul_floor(collateral_value_lovelace, best_threshold)


class DanogoLoanState(AbstractLoanState):
    """Snapshot of one Danogo loan position."""

    address: str
    _pool: DanogoPoolState | None = PrivateAttr(default=None)
    _market: DanogoMarket | None = PrivateAttr(default=None)
    _now_ms: int = PrivateAttr(default=0)

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "Danogo"

    @classmethod
    def loan_datum_class(cls) -> type[PlutusData]:
        """Loan datum type."""
        return LoanDatum

    @classmethod
    def loan_selector(cls) -> PoolSelector:
        """Loan UTxOs are discovered by the loader; selector built there."""
        return PoolSelector(addresses=[], assets=None)

    def attach_context(
        self,
        *,
        pool: DanogoPoolState,
        market: DanogoMarket,
        now_ms: int,
    ) -> None:
        """Wire the pool/market this loan belongs to + the snapshot time."""
        self._pool = pool
        self._market = market
        self._now_ms = now_ms

    @property
    def stake_address(self) -> Address:
        """Script address of the loan UTxO."""
        return Address.decode(self.address)

    @property
    def loan_id(self) -> str:
        """Owner-NFT-based identifier."""
        d: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return d.owner_nft.unit()

    @property
    def pool_id(self) -> str:
        """Id of the pool this loan belongs to."""
        if self._pool is None:
            raise ValueError("pool not attached")
        return self._pool.pool_id

    @property
    def borrowed_unit(self) -> str:
        """Borrowed asset unit (the market supply token)."""
        d: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return d.token_unit()

    def _collateral_units(self) -> dict[str, int]:
        market = self._market
        if market is None:
            raise ValueError("market not attached")
        return {
            unit: amount
            for unit, amount in self.assets.root.items()
            if unit in market.collaterals
        }

    def _collateral_terms(
        self,
        prices: PriceMap,
    ) -> tuple[list[tuple[int, int, int, int]], bool]:
        """Threshold-weighted collateral terms + whether any price is missing.

        Returns (terms, has_missing) where each term is
        (amount, price_num, price_denom, threshold_bps) for collateral units that
        have a usable price, and `has_missing` is True if any collateral unit this
        loan holds has no usable oracle price (absent, or non-positive num/denom).
        A missing price means the position cannot be safely assessed.
        """
        market = self._market
        if market is None:
            raise ValueError("market not attached")
        terms: list[tuple[int, int, int, int]] = []
        has_missing = False
        for unit, amount in self._collateral_units().items():
            price = prices.get(unit, quote=market.supply_token)
            if price is None or price.num <= 0 or price.denom <= 0:
                has_missing = True
                continue
            terms.append((amount, price.num, price.denom, market.threshold_for(unit)))
        return terms, has_missing

    def current_debt(self) -> int:
        """Principal + accrued interest to the snapshot time."""
        if self._pool is None:
            raise ValueError("pool not attached")
        pool: PoolDatum = self._pool.pool_datum  # type: ignore[assignment]
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        idx = current_interest_index(
            pool.interest_index,
            borrow_apy=pool.borrow_apy,
            interest_time=pool.interest_time,
            txn_time=self._now_ms,
        )
        return current_loan_amount(
            loan_amount=ld.loan_amount,
            current_index=idx,
            initial_index=ld.initial_interest_index,
        )

    def collateral_value_lovelace(self, prices: PriceMap) -> int:
        """Collateral value in the borrowed (supply) unit at given prices.

        Unpriced collateral is omitted; callers needing a trustworthy value should
        check `is_liquidatable` / pricing completeness separately.
        """
        terms, _ = self._collateral_terms(prices)
        return total_collateral_val([(a, n, d) for a, n, d, _ in terms])

    def health_factor(self, prices: PriceMap) -> Decimal:
        """weighted-collateral / debt; liquidatable at <= 1.

        Computed over the collateral that has usable prices. When some collateral
        is unpriced, prefer `is_liquidatable`, which refuses to flag a position it
        cannot fully assess.
        """
        debt = self.current_debt()
        if debt <= 0:
            return Decimal("Infinity")
        terms, _ = self._collateral_terms(prices)
        weighted = total_collateral_val_with_threshold(terms)
        return Decimal(weighted) / Decimal(debt)

    def is_liquidatable(self, prices: PriceMap) -> bool:
        """Liquidatable only when fully priced AND health factor <= 1.

        If any collateral unit is unpriced we cannot assess the position, so we do
        NOT flag it liquidatable (avoids false positives during oracle outages).
        """
        terms, has_missing = self._collateral_terms(prices)
        if has_missing:
            return False
        return self.health_factor(prices) <= 1

    def oracle_refs(self) -> list[OracleRef]:
        """One aggregator ref per collateral unit (priced into supply_token)."""
        market = self._market
        if market is None:
            return []
        refs = []
        for unit in self._collateral_units():
            refs.append(
                OracleRef(
                    source=OracleSource.DANOGO_AGGREGATOR,
                    token=unit,
                    quote=market.supply_token,
                ),
            )
        return refs
