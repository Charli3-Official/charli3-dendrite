"""Read-only lending pool/loan state bases + aggregation views."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import UTxO

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.models import get_resolver

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend


class AbstractLendingPoolState(DendriteBaseModel, ABC):
    """Snapshot of a lending pool/market (read-only)."""

    assets: Assets
    block_time: int
    block_index: int
    plutus_v2: bool
    datum_cbor: str
    datum_hash: str
    tx_index: int
    tx_hash: str
    inactive: bool = False

    _pool_datum_parsed: PlutusData | None = None

    @classmethod
    @abstractmethod
    def protocol(cls) -> str:
        """Official protocol name (e.g. 'Danogo', 'FluidTokens')."""
        raise NotImplementedError

    @classmethod
    def pool_policy(cls) -> list[str] | None:
        """Pool-NFT policy (or policy+name) used to recognize the pool."""
        return None

    @classmethod
    @abstractmethod
    def pool_datum_class(cls) -> type[PlutusData]:
        """Plutus type for the pool datum."""
        raise NotImplementedError

    @classmethod
    def oracle_datum_class(cls) -> type[PlutusData] | None:
        """Optional protocol-owned oracle datum type."""
        return None

    @classmethod
    @abstractmethod
    def pool_selector(cls) -> PoolSelector:
        """UTxO selector for this pool/market."""
        raise NotImplementedError

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Optional protocol reference-script UTxO."""
        return None

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script | PlutusV2Script]:
        """Default Plutus script version for this protocol."""
        return PlutusV1Script

    def script_class(self) -> type[PlutusV1Script | PlutusV2Script]:
        """Plutus script version implied by this UTxO."""
        return PlutusV2Script if self.plutus_v2 else PlutusV1Script

    @property
    @abstractmethod
    def stake_address(self) -> Address:
        """Stake address controlling this pool."""
        raise NotImplementedError

    @property
    @abstractmethod
    def pool_id(self) -> str:
        """Stable identifier for this pool/market."""
        raise NotImplementedError

    @property
    def pool_datum(self) -> PlutusData:
        """Parsed pool datum (lazily decoded from CBOR)."""
        if self._pool_datum_parsed is None:
            self._pool_datum_parsed = self.pool_datum_class().from_cbor(self.datum_cbor)
        return self._pool_datum_parsed

    @property
    @abstractmethod
    def borrowable_unit(self) -> str:
        """Unit that can be borrowed from this pool."""
        raise NotImplementedError

    @property
    @abstractmethod
    def utilization_ratio(self) -> Decimal:
        """Fraction of supplied liquidity currently borrowed."""
        raise NotImplementedError

    def oracle_refs(self) -> list[OracleRef]:
        """Oracle feeds this pool needs (e.g. alt-token rates). Default none."""
        return []

    @abstractmethod
    def max_borrow_for_collateral_value(self, collateral_value_lovelace: int) -> int:
        """Best-effort quote: collateral value -> borrowable size."""
        raise NotImplementedError


class AbstractLoanState(DendriteBaseModel, ABC):
    """Snapshot of a single borrower position (read-only)."""

    assets: Assets
    block_time: int
    block_index: int
    plutus_v2: bool
    datum_cbor: str
    datum_hash: str
    tx_index: int
    tx_hash: str
    inactive: bool = False

    _loan_datum_parsed: PlutusData | None = None

    @classmethod
    @abstractmethod
    def protocol(cls) -> str:
        """Official protocol name (e.g. 'Danogo', 'FluidTokens')."""
        raise NotImplementedError

    @classmethod
    def protocol_nft_policy(cls) -> list[str] | None:
        """Policy / policy+name prefixes for the loan-identity NFT."""
        return None

    @classmethod
    @abstractmethod
    def loan_datum_class(cls) -> type[PlutusData]:
        """Plutus type for the loan datum."""
        raise NotImplementedError

    @classmethod
    def oracle_datum_class(cls) -> type[PlutusData] | None:
        """Optional protocol-owned oracle datum type."""
        return None

    @classmethod
    @abstractmethod
    def loan_selector(cls) -> PoolSelector:
        """UTxO selector for loans (same shape as pool_selector)."""
        raise NotImplementedError

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Optional protocol reference-script UTxO."""
        return None

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script | PlutusV2Script]:
        """Default Plutus script version for this protocol."""
        return PlutusV1Script

    def script_class(self) -> type[PlutusV1Script | PlutusV2Script]:
        """Plutus script version implied by this UTxO."""
        return PlutusV2Script if self.plutus_v2 else PlutusV1Script

    @property
    @abstractmethod
    def stake_address(self) -> Address:
        """Stake address controlling this loan."""
        raise NotImplementedError

    @property
    @abstractmethod
    def loan_id(self) -> str:
        """Stable identifier for this loan."""
        raise NotImplementedError

    @property
    @abstractmethod
    def pool_id(self) -> str:
        """Identifier of the pool/market this loan belongs to."""
        raise NotImplementedError

    @property
    def loan_datum(self) -> PlutusData:
        """Parsed loan datum (lazily decoded from CBOR)."""
        if self._loan_datum_parsed is None:
            self._loan_datum_parsed = self.loan_datum_class().from_cbor(self.datum_cbor)
        return self._loan_datum_parsed

    @property
    @abstractmethod
    def borrowed_unit(self) -> str:
        """Unit borrowed by this loan."""
        raise NotImplementedError

    def oracle_refs(self) -> list[OracleRef]:
        """Oracle feeds needed to value this loan. Default none (fixed-ratio)."""
        return []

    @abstractmethod
    def current_debt(self) -> int:
        """Principal + accrued interest, in smallest units of borrowed asset."""
        raise NotImplementedError

    @abstractmethod
    def collateral_value_lovelace(self, prices: PriceMap) -> int:
        """Collateral value in lovelace at the given prices."""
        raise NotImplementedError

    @abstractmethod
    def health_factor(self, prices: PriceMap) -> Decimal:
        """Liquidation ratio; default rule liquidatable when <= 1."""
        raise NotImplementedError

    def is_liquidatable(self, prices: PriceMap) -> bool:
        """Default: health_factor <= 1. Override if on-chain rule differs."""
        return self.health_factor(prices) <= 1

    def repay_amount_for_target_hf(self, target_hf: Decimal, prices: PriceMap) -> int:
        """Repayment to reach `target_hf`.

        Generic helper assuming the borrowed asset is denominated in lovelace
        (i.e. collateral_value_lovelace and debt share units). Protocols whose
        borrowed asset is not ADA should override.
        """
        if target_hf <= 0:
            return self.current_debt()
        debt = Decimal(self.current_debt())
        max_safe_debt = Decimal(self.collateral_value_lovelace(prices)) / target_hf
        if debt <= max_safe_debt:
            return 0
        return int((debt - max_safe_debt).to_integral_value(rounding="ROUND_CEILING"))


class LendingPriceBook:
    """Batch oracle resolution for a set of loans/pools.

    Selectors are de-duplicated within each source group, so each unique feed
    is fetched once (one fetch per unique selector per source) no matter how
    many loans reference it.
    """

    @staticmethod
    def build(
        items: Sequence[AbstractLoanState | AbstractLendingPoolState],
        *,
        backend: AbstractBackend,
        at_ms: int | None = None,  # noqa: ARG004
    ) -> PriceMap:
        """Resolve every source referenced by `items` into one `PriceMap`."""
        refs: list[OracleRef] = []
        for item in items:
            refs.extend(item.oracle_refs())

        by_source: dict[OracleSource, list[OracleRef]] = {}
        for ref in refs:
            by_source.setdefault(ref.source, []).append(ref)

        price_map = PriceMap()
        for source, group in by_source.items():
            resolver = get_resolver(source)
            seen: set[tuple] = set()
            infos: list[PoolStateInfo] = []
            for selector in resolver.selectors(group):
                key = (
                    tuple(sorted(selector.addresses)),
                    tuple(sorted(selector.assets or ())),
                )
                if key in seen:
                    continue
                seen.add(key)
                infos.extend(list(backend.get_pool_utxos(**selector.model_dump())))
            utxos = PoolStateList(root=infos)
            for ref in group:
                price = resolver.resolve(ref, utxos)
                if price is not None:
                    price_map.add(price)
        return price_map


class LendingBook:
    """Aggregated, price-aware view of loans for bots and risk analytics."""

    def __init__(
        self,
        loans_full: list[AbstractLoanState],
        *,
        block_time: int,
        prices: PriceMap | None = None,
        pool: AbstractLendingPoolState | None = None,
    ) -> None:
        """Store loans plus optional prices/pool context for aggregation."""
        self.loans_full = list(loans_full)
        self.block_time = block_time
        self.prices = prices or PriceMap()
        self.pool = pool

    @classmethod
    def from_loans(
        cls,
        loans: Sequence[AbstractLoanState],
        *,
        prices: PriceMap | None = None,
        pool: AbstractLendingPoolState | None = None,
        block_time: int | None = None,
    ) -> LendingBook:
        """Construct a book from `loans`, defaulting `block_time` to the latest."""
        bt = (
            block_time
            if block_time is not None
            else max((ln.block_time for ln in loans), default=0)
        )
        return cls(list(loans), block_time=bt, prices=prices, pool=pool)

    def active_loans(self) -> list[AbstractLoanState]:
        """Loans that are not marked inactive."""
        return [ln for ln in self.loans_full if not ln.inactive]

    def liquidatable_loans(self) -> list[AbstractLoanState]:
        """Active loans whose health factor makes them liquidatable."""
        return [ln for ln in self.active_loans() if ln.is_liquidatable(self.prices)]

    def liquidation_candidates_by_urgency(self) -> list[AbstractLoanState]:
        """Liquidatable loans sorted by ascending health factor."""
        scored = [
            (ln, ln.health_factor(self.prices)) for ln in self.liquidatable_loans()
        ]
        scored.sort(key=lambda item: (item[1], item[0].loan_id))
        return [ln for ln, _ in scored]

    def near_liquidation(
        self,
        buffer: Decimal = Decimal("0.05"),
    ) -> list[AbstractLoanState]:
        """Healthy active loans within `buffer` above the liquidation threshold."""
        hi = Decimal(1) + buffer
        scored = [
            (ln, ln.health_factor(self.prices))
            for ln in self.active_loans()
            if not ln.is_liquidatable(self.prices)
        ]
        scored = [(ln, hf) for ln, hf in scored if hf <= hi]
        scored.sort(key=lambda item: (item[1], item[0].loan_id))
        return [ln for ln, _ in scored]

    def by_pool(self, pool_id: str) -> list[AbstractLoanState]:
        """Active loans belonging to `pool_id`."""
        return [ln for ln in self.active_loans() if ln.pool_id == pool_id]
