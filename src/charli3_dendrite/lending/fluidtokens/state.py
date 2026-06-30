"""FluidTokens V3 read-only pool / loan / request state.

A FluidTokens "market" is the loan-terms view derived from a pool datum
(`FluidMarket`); pools, loans, and requests are distinct on-chain UTxOs. Pools and
loans implement the shared lending bases (`AbstractLendingPoolState` /
`AbstractLoanState`); a request is neither, so it is a lightweight read-only view.
"""

from __future__ import annotations

from decimal import Decimal

from pycardano import Address
from pycardano import PlutusData
from pydantic import PrivateAttr

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.lending.base import AbstractLendingPoolState
from charli3_dendrite.lending.base import AbstractLoanState
from charli3_dendrite.lending.fluidtokens.constants import LOAN_POLICY
from charli3_dendrite.lending.fluidtokens.constants import POOL_POLICY
from charli3_dendrite.lending.fluidtokens.constants import REQUEST_POLICY
from charli3_dendrite.lending.fluidtokens.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
from charli3_dendrite.lending.fluidtokens.market import FluidMarket
from charli3_dendrite.lending.fluidtokens.math import health_factor as hf_math
from charli3_dendrite.lending.fluidtokens.math import installments_pi_amount
from charli3_dendrite.lending.fluidtokens.math import perpetual_outstanding_debt
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.units import asset_unit
from charli3_dendrite.lending.units import constr

# RepaymentMode alternatives; LiquidationMode alt 2 is the LTV-threshold variant;
# the Plutus `Bool` True / Option `Some` variants are alt 1 / alt 0.
_REPAYMENT_INTEREST_ON_PRINCIPAL = 0
_REPAYMENT_INSTALLMENTS = 1
_REPAYMENT_PERPETUAL = 2
_LIQUIDATION_THRESHOLD = 2
_BOOL_TRUE = 1
_OPTION_SOME = 0


def _collateral_unit(collateral) -> str:  # noqa: ANN001 - datums.CollateralAsset
    """Dendrite unit of a `CollateralAsset` (Option-None name -> policy only)."""
    name_alt, name_fields = constr(collateral.maybe_asset_name)
    name = name_fields[0] if name_alt == _OPTION_SOME else b""
    return asset_unit(collateral.policy_id, name)


def _unit_with_policy(assets: Assets, policy: str) -> str | None:
    """First asset unit in `assets` whose policy id (first 56 hex) matches."""
    for unit in assets.root:
        if unit != "lovelace" and unit[:56] == policy:
            return unit
    return None


class FluidPoolState(AbstractLendingPoolState):
    """Snapshot of one FluidTokens pool (lender liquidity + loan terms)."""

    address: str
    _market: FluidMarket | None = PrivateAttr(default=None)

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "FluidTokens"

    @classmethod
    def pool_datum_class(cls) -> type[PlutusData]:
        """Pool datum type."""
        return PoolDatum

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool UTxOs are discovered by the loader; selector built there."""
        return PoolSelector(addresses=[], assets=None)

    def attach_market(self, market: FluidMarket) -> None:
        """Attach the market (loan-terms view) derived from this pool's datum."""
        self._market = market

    @property
    def market(self) -> FluidMarket:
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
        """Stable id: the pool-NFT unit held by the UTxO (address fallback)."""
        unit = _unit_with_policy(self.assets, POOL_POLICY)
        return unit if unit is not None else self.address

    @property
    def borrowable_unit(self) -> str:
        """Unit borrowers borrow (the market principal asset)."""
        return self.market.principal_unit

    @property
    def utilization_ratio(self) -> Decimal:
        """Always 0: FluidTokens pools do not track supply/borrow in the datum.

        Each pool's available liquidity is simply the value held by its UTxO, and
        utilization is not recorded on-chain, so there is no datum field to derive a
        borrowed/supplied fraction from. Returning 0 keeps the abstract contract
        satisfied for read-only analytics without inventing a figure.
        """
        return Decimal(0)

    def oracle_refs(self) -> list[OracleRef]:
        """Pools carry no oracle refs; collateral pricing is driven per-loan."""
        return []

    def max_borrow_for_collateral_value(self, collateral_value_lovelace: int) -> int:
        """Best-effort: apply the pool's liquidation LTV to a collateral value."""
        ltv = self.market.liquidation_ltv
        if ltv is None:
            return 0
        l_tv, divider = ltv
        if divider <= 0:
            return 0
        return collateral_value_lovelace * l_tv // divider


class FluidLoanState(AbstractLoanState):
    """Snapshot of one FluidTokens borrower position."""

    address: str
    _pool: FluidPoolState | None = PrivateAttr(default=None)
    _market: FluidMarket | None = PrivateAttr(default=None)
    _now_ms: int = PrivateAttr(default=0)

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return "FluidTokens"

    @classmethod
    def protocol_nft_policy(cls) -> list[str] | None:
        """Loan-identity NFT policy."""
        return [LOAN_POLICY]

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
        pool: FluidPoolState,
        market: FluidMarket,
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
        """Loan-NFT-based identifier (the LOAN_POLICY unit held by the UTxO)."""
        unit = _unit_with_policy(self.assets, LOAN_POLICY)
        return unit if unit is not None else ""

    @property
    def pool_id(self) -> str:
        """Id of the pool this loan belongs to (datum origin id as fallback)."""
        if self._pool is not None:
            return self._pool.pool_id
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return ld.origin_id.hex()

    @property
    def borrowed_unit(self) -> str:
        """Borrowed asset unit (the loan principal asset)."""
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return ld.principal_asset.unit()

    def current_debt(self) -> int:
        """Principal + accrued interest to the snapshot time.

        Dispatches on the loan's repayment-mode constructor. Only the perpetual mode
        (alt 2) has live on-chain fixtures; the interest-on-principal (alt 0) and
        installments (alt 1) modes use a best-effort installments-based estimate and
        are unvalidated against on-chain behavior (no live fixture yet). None of the
        branches raise.
        """
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        mode_alt, mode_fields = constr(ld.repayment_mode)
        if mode_alt == _REPAYMENT_PERPETUAL:
            return perpetual_outstanding_debt(
                principal=ld.principal_amount,
                interest_rate=ld.interest_rate,
                apy_coef=int(mode_fields[0]),
                lend_date_ms=ld.lend_date,
                now_ms=self._now_ms,
            )
        # Best-effort for the non-perpetual modes (no live fixture; unvalidated):
        # total principal+interest spread across the installment schedule, minus the
        # installments already repaid.
        installments = ld.total_installments
        per_installment = installments_pi_amount(
            principal=ld.principal_amount,
            interest_rate=ld.interest_rate,
            total_installments=installments,
        )
        if mode_alt in (_REPAYMENT_INTEREST_ON_PRINCIPAL, _REPAYMENT_INSTALLMENTS):
            remaining = max(installments - ld.repaid_installments, 0)
            if installments <= 0:
                return per_installment
            return per_installment * remaining
        return ld.principal_amount

    def _collateral_unit(self) -> str:
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return _collateral_unit(ld.collateral)

    def collateral_value_lovelace(self, prices: PriceMap) -> int:
        """Collateral value in the principal unit at given prices.

        The loan holds a single collateral asset; its amount is read from the UTxO
        value and priced into the principal unit. An unpriced collateral yields 0
        (mirrors danogo: callers needing a trustworthy value check `is_liquidatable`).
        """
        unit = self._collateral_unit()
        amount = self.assets[unit]
        price = prices.get(unit)
        if price is None or price.num <= 0 or price.denom <= 0:
            return 0
        return amount * price.num // price.denom

    def _liquidation_ltv(self) -> tuple[int, int] | None:
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        liq_alt, liq_fields = constr(ld.liquidation_mode)
        if liq_alt != _LIQUIDATION_THRESHOLD:
            return None
        return int(liq_fields[0]), int(liq_fields[1])

    def health_factor(self, prices: PriceMap) -> Decimal:
        """(collateral * lTV) / (debt * divider); liquidatable at <= 1.

        A NoLiquidation* mode (alt 0/1) cannot be liquidated, so it is infinitely
        healthy. When some collateral is unpriced, prefer `is_liquidatable`, which
        refuses to flag a position it cannot fully assess.
        """
        debt = self.current_debt()
        if debt <= 0:
            return Decimal("Infinity")
        ltv = self._liquidation_ltv()
        if ltv is None:
            return Decimal("Infinity")
        l_tv, divider = ltv
        return hf_math(
            collateral_value=Decimal(self.collateral_value_lovelace(prices)),
            debt=Decimal(debt),
            l_tv=l_tv,
            l_tv_divider=divider,
        )

    def is_liquidatable(self, prices: PriceMap) -> bool:
        """Liquidatable only when fully priced AND health factor <= 1.

        If the collateral unit has no usable price we cannot assess the position, so
        we do NOT flag it (avoids false positives during oracle outages).
        """
        unit = self._collateral_unit()
        price = prices.get(unit)
        if price is None or price.num <= 0 or price.denom <= 0:
            return False
        return self.health_factor(prices) <= 1

    def oracle_refs(self) -> list[OracleRef]:
        """One aggregated collateral ref when the loan is dynamically priced.

        The precise feed source (Aggregated / Dedicated / Charli3 / Orcfax) is
        refined by the loader/feeds in a later task; here a dynamic loan emits a
        single `FLUID_AGGREGATED` ref for its collateral priced into the principal.
        """
        if self._market is None or not self._market.is_dynamic:
            return []
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return [
            OracleRef(
                source=OracleSource.FLUID_AGGREGATED,
                token=self._collateral_unit(),
                quote=ld.principal_asset.unit(),
            ),
        ]


class FluidRequestState(DendriteBaseModel):
    """Lightweight read-only view of a FluidTokens borrow request UTxO.

    A request is a borrower's open offer (collateral + desired principal terms); it
    is neither a pool nor a loan, so it does not implement the lending bases.
    """

    address: str
    assets: Assets
    datum_cbor: str

    _request_datum_parsed: RequestDatum | None = PrivateAttr(default=None)

    @property
    def request_datum(self) -> RequestDatum:
        """Parsed request datum (lazily decoded from CBOR)."""
        if self._request_datum_parsed is None:
            self._request_datum_parsed = RequestDatum.from_cbor(self.datum_cbor)
        return self._request_datum_parsed

    @property
    def request_id(self) -> str:
        """Request-NFT-based identifier (the REQUEST_POLICY unit held)."""
        unit = _unit_with_policy(self.assets, REQUEST_POLICY)
        return unit if unit is not None else self.address

    @property
    def max_principal(self) -> int:
        """Maximum principal the borrower is requesting."""
        return self.request_datum.max_principal

    @property
    def min_principal(self) -> int:
        """Minimum principal the borrower is requesting."""
        return self.request_datum.min_principal

    @property
    def collateral_unit(self) -> str:
        """Collateral asset unit offered (Option-None name -> policy only)."""
        return _collateral_unit(self.request_datum.collateral)

    @property
    def principal_unit(self) -> str:
        """Requested principal asset unit."""
        return self.request_datum.common_data.principal_asset.unit()

    @property
    def is_dynamic(self) -> bool:
        """Whether collateral is dynamically priced (Bool True == alt 1)."""
        return constr(self.request_datum.dynamic_collateral_price)[0] == _BOOL_TRUE

    @property
    def request_expiration(self) -> int:
        """POSIX-ms expiration after which the request is no longer fillable."""
        return self.request_datum.request_expiration
