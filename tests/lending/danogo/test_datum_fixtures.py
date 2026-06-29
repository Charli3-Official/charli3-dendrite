"""Lock Danogo datum layouts against real mainnet CBOR.

These parse public on-chain datum bytes captured from the live Danogo Float
deployment (see ``fixtures/mainnet_datums.json``). They run fully offline and
guard against accidental drift in the datum models.
"""

import json
from pathlib import Path

import pytest

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.oracles.aggregator_datums import (
    OracleGlobalConfig,
)
from charli3_dendrite.lending.danogo.oracles.aggregator_datums import OraclePathDatum

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mainnet_datums.json").read_text()
)


def test_pool_datum_parses_live():
    d = PoolDatum.from_cbor(FIXTURES["pool_state"]["datum"])
    # ADA market snapshot: precomputed borrow APY (bps) and non-trivial totals.
    assert d.borrow_apy == 307
    assert d.total_supply > d.total_borrow > 0
    assert d.circulating_dtoken > 0
    # Two alt supply tokens (matches the market's alt_supply_tokens map).
    assert len(d.alt_supply_tokens_rate) == 2


def test_loan_datums_parse_live():
    loans = FIXTURES["loans"]
    assert loans, "expected captured loans"
    for ln in loans:
        d = LoanDatum.from_cbor(ln["datum"])
        assert d.loan_amount > 0
        assert d.initial_interest_index > 0
        # Borrowed token + owner NFT resolve to non-empty dendrite units.
        assert d.token_unit()
        assert len(d.owner_nft.unit()) >= 56  # at least a 28-byte policy hex


def test_market_datum_parses_live():
    m = DanogoMarket.from_market_datum(FIXTURES["market_param"]["datum"])
    assert m.supply_token == "lovelace"  # ADA market
    assert len(m.collaterals) == 44
    assert len(m.alt_supply_tokens) == 2
    # Every collateral carries a sane liquidation threshold (bps).
    assert all(0 < t <= 10_000 for t in m.collaterals.values())
    # Interest-rate curve params decoded from their fixed positions.
    assert m.base_rate == 200
    assert m.power_base == 10_250
    assert m.util_cap == 9_000


def test_oracle_path_datums_parse_live():
    paths = FIXTURES["oracle_paths"]
    assert paths, "expected captured oracle paths"
    for p in paths:
        d = OraclePathDatum.from_cbor(p["datum"])
        assert len(d.anchor) == 28
        assert d.deviation_bps == 500
        # Two byte-packed spec lists (per-entry sub-structure decode is incremental).
        assert d.price_paths and d.oracle_sources


def test_oracle_global_config_parses_live():
    g = OracleGlobalConfig.from_cbor(FIXTURES["oracle_global_config"]["datum"])
    # The datum body is a packed byte blob, not a Constr.
    assert isinstance(g.raw, bytes)
    assert len(g.raw) == 87


@pytest.mark.parametrize("loan", FIXTURES["loans"])
def test_loan_token_units_are_known_assets(loan):
    d = LoanDatum.from_cbor(loan["datum"])
    unit = d.token_unit()
    # Borrowed unit is either ADA or a policy(56 hex)+name asset id.
    assert unit == "lovelace" or len(unit) >= 56
