import json
from pathlib import Path

import cbor2
from pycardano import RawPlutusData

from charli3_dendrite.lending.fluidtokens.datums import Asset
from charli3_dendrite.lending.fluidtokens.datums import CollateralAsset
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens.market import FluidMarket

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())

# Plutus Option None / Bool False / nullary LiquidationMode all serialize as the
# empty Constr1 (CBOR tag 122).
_NONE = RawPlutusData(cbor2.CBORTag(122, []))


def _pool_with(*, liquidation_mode, collateral_options):
    """A `PoolDatum` from the fixture with select fields overridden."""
    d = PoolDatum.from_cbor(FIX["pool"]["datum_cbor"])
    d.common_data.liquidation_mode = liquidation_mode
    d.collateral_options = collateral_options
    return d


def test_market_from_pool_datum():
    m = FluidMarket.from_pool_datum(PoolDatum.from_cbor(FIX["pool"]["datum_cbor"]))
    assert m.principal_unit == "lovelace"
    assert m.interest_rate == 400
    assert m.is_perpetual is True
    assert m.is_dynamic is True
    assert m.liquidation_ltv == (100, 125)  # (lTV, divider)
    assert len(m.collateral_units) == 10


def test_collateral_with_no_asset_name():
    """An Option-None collateral name yields a unit of just the policy hex."""
    policy = bytes.fromhex("00" * 28)
    collateral = CollateralAsset(
        policy_id=policy,
        maybe_asset_name=_NONE,
        oracle_token_asset=Asset(policy_id=b"", asset_name=b""),
    )
    base = PoolDatum.from_cbor(FIX["pool"]["datum_cbor"])
    m = FluidMarket.from_pool_datum(
        _pool_with(
            liquidation_mode=base.common_data.liquidation_mode,
            collateral_options=[collateral],
        ),
    )
    assert m.collateral_units == [policy.hex()]


def test_no_liquidation_mode_yields_none_ltv():
    """A NoLiquidation* constructor (alt 0/1) maps to ``liquidation_ltv is None``."""
    base = PoolDatum.from_cbor(FIX["pool"]["datum_cbor"])
    for tag in (121, 122):  # alt 0 / alt 1 nullary constructors
        d = _pool_with(
            liquidation_mode=RawPlutusData(cbor2.CBORTag(tag, [])),
            collateral_options=base.collateral_options,
        )
        assert FluidMarket.from_pool_datum(d).liquidation_ltv is None
