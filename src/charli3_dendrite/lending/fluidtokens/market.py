"""Typed view over the FluidTokens V3 pool terms."""

from __future__ import annotations

from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.units import asset_unit
from charli3_dendrite.lending.units import constr

# RepaymentMode alt 2 is PerpetualLoan; LiquidationMode alt 2 is Liquidation; the
# Plutus `Bool` True variant is alt 1; the Option `Some` variant is alt 0.
_REPAYMENT_PERPETUAL = 2
_LIQUIDATION_THRESHOLD = 2
_BOOL_TRUE = 1
_OPTION_SOME = 0


class FluidMarket(DendriteBaseModel):
    """Plain-Python view of a FluidTokens pool's loan terms."""

    principal_unit: str
    interest_rate: int
    is_perpetual: bool
    is_dynamic: bool
    liquidation_ltv: tuple[int, int] | None  # (l_tv, l_tv_divider)
    collateral_units: list[str]

    @classmethod
    def from_pool_datum(cls, d: PoolDatum) -> FluidMarket:
        """Derive a `FluidMarket` from a parsed `PoolDatum`."""
        common = d.common_data

        liq_alt, liq_fields = constr(common.liquidation_mode)
        liquidation_ltv = (
            (int(liq_fields[0]), int(liq_fields[1]))
            if liq_alt == _LIQUIDATION_THRESHOLD
            else None
        )

        collateral_units: list[str] = []
        for c in d.collateral_options:
            name_alt, name_fields = constr(c.maybe_asset_name)
            name = name_fields[0] if name_alt == _OPTION_SOME else b""
            collateral_units.append(asset_unit(c.policy_id, name))

        return cls(
            principal_unit=common.principal_asset.unit(),
            interest_rate=common.interest_rate,
            is_perpetual=constr(common.repayment_mode)[0] == _REPAYMENT_PERPETUAL,
            is_dynamic=constr(d.dynamic_collateral_price)[0] == _BOOL_TRUE,
            liquidation_ltv=liquidation_ltv,
            collateral_units=collateral_units,
        )
