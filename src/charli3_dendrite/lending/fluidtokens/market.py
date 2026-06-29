"""Typed view over the FluidTokens V3 pool terms."""

from __future__ import annotations

import cbor2  # type: ignore[import-not-found]
from pycardano import RawPlutusData

from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.units import asset_unit
from charli3_dendrite.lending.units import constr_alt

# Tag 102 is the general Constr form whose value is ``[alt, fields]``; the simple
# tag->alt ranges are resolved by the shared ``constr_alt``.
_CONSTR_GENERAL_TAG = 102

# RepaymentMode alt 2 is PerpetualLoan; LiquidationMode alt 2 is Liquidation; the
# Plutus `Bool` True variant is alt 1; the Option `Some` variant is alt 0.
_REPAYMENT_PERPETUAL = 2
_LIQUIDATION_THRESHOLD = 2
_BOOL_TRUE = 1
_OPTION_SOME = 0


def _constr(raw: object) -> tuple[int, list]:
    """(alternative index, positional fields) of a decoded Plutus Constr.

    Accepts a ``RawPlutusData`` (union datum field), a ``cbor2.CBORTag``, or a typed
    ``PlutusData`` (exposing ``CONSTR_ID`` + fields as attributes). Raises
    ``ValueError`` for a CBOR tag outside the known constructor ranges.
    """
    if isinstance(raw, RawPlutusData):
        raw = raw.data
    if isinstance(raw, cbor2.CBORTag):
        tag, value = raw.tag, raw.value
        if tag == _CONSTR_GENERAL_TAG:
            return int(value[0]), list(value[1])
        return constr_alt(tag), list(value)
    return getattr(raw, "CONSTR_ID", 0), list(getattr(raw, "__dict__", {}).values())


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

        liq_alt, liq_fields = _constr(common.liquidation_mode)
        liquidation_ltv = (
            (int(liq_fields[0]), int(liq_fields[1]))
            if liq_alt == _LIQUIDATION_THRESHOLD
            else None
        )

        collateral_units: list[str] = []
        for c in d.collateral_options:
            name_alt, name_fields = _constr(c.maybe_asset_name)
            name = name_fields[0] if name_alt == _OPTION_SOME else b""
            collateral_units.append(asset_unit(c.policy_id, name))

        return cls(
            principal_unit=common.principal_asset.unit(),
            interest_rate=common.interest_rate,
            is_perpetual=_constr(common.repayment_mode)[0] == _REPAYMENT_PERPETUAL,
            is_dynamic=_constr(d.dynamic_collateral_price)[0] == _BOOL_TRUE,
            liquidation_ltv=liquidation_ltv,
            collateral_units=collateral_units,
        )
