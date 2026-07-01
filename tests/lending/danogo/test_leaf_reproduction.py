"""End-to-end offline reproduction of on-chain Danogo aggregator prices.

Reconstructs the collateral prices from a real ``OraclePriceCalcRdmr`` purely from
the leaf oracle-source datums it referenced -- exercising the verified leaf pricers
(`leaves`) and the deterministic hop combiner (`aggregator.derive_path_price`). The
target values are the exact rationals the on-chain validator wrote into the redeemer,
so a passing test is bit-for-bit agreement with mainnet.
"""

import json
from fractions import Fraction
from pathlib import Path

from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.oracles.aggregator import derive_path_price
from charli3_dendrite.lending.danogo.oracles.leaves import cpamm_price_from_reserves
from charli3_dendrite.lending.danogo.oracles.leaves import danogo_pool_dtoken_rate
from charli3_dendrite.lending.danogo.oracles.leaves import parse_djed
from charli3_dendrite.lending.danogo.oracles.leaves import parse_liqwid_oracle_v2
from charli3_dendrite.lending.danogo.oracles.leaves import parse_minswap_lp
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr

_FIX = Path(__file__).parent / "fixtures" / "mainnet_datums.json"
_DATA = json.loads(_FIX.read_text())
_LEAVES = _DATA["oracle_leaves"]
_TX = "929a7d68e0f05db06677c8bb675cd36be6a5d5fb23b5ddd008d5af4952de8237"
_REDEEMER = next(e["redeemer"] for e in _DATA["oracle_redeemers"] if e["tx"] == _TX)
_QUOTE = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d"
_ADA = "lovelace"


def _onchain_prices():
    r = OraclePriceCalcRdmr.from_cbor(_REDEEMER)
    return {c: Fraction(n, d) for c, (n, d) in r.prices[_QUOTE].items()}


def _pool_rate(datum: str) -> tuple[int, int]:
    pd = PoolDatum.from_cbor(datum)
    return danogo_pool_dtoken_rate(
        total_supply=pd.total_supply, circulating_dtoken=pd.circulating_dtoken
    )


def test_liqwid_oracle_reproduces_ada_price():
    prices = _onchain_prices()
    asset, (num, denom) = parse_liqwid_oracle_v2(_LEAVES["liqwid_oracle_v2"])
    assert asset == _ADA
    assert Fraction(num, denom) == prices[_ADA]


def test_danogo_pool_reproduces_dtoken_collateral_price():
    prices = _onchain_prices()
    num, denom = _pool_rate(_LEAVES["danogo_pool_ref2"])
    # The single-hop Danogo-pool collateral price in this tx.
    assert Fraction(num, denom) in prices.values()


def test_three_hop_strike_collateral_reproduced_bit_exact():
    """dSTRIKE -> STRIKE (Danogo) -> ADA (Minswap) -> USD (Liqwid)."""
    prices = _onchain_prices()

    h1_num, h1_denom = _pool_rate(_LEAVES["danogo_pool_out1"])

    _, reserve_a, _, reserve_b = parse_minswap_lp(_LEAVES["minswap_lp"])
    # STRIKE (asset_b) -> ADA (asset_a): quote=ADA reserve, token=STRIKE reserve.
    h2_num, h2_denom = cpamm_price_from_reserves(
        reserve_token=reserve_b, reserve_quote=reserve_a
    )

    _, (h3_num, h3_denom) = parse_liqwid_oracle_v2(_LEAVES["liqwid_oracle_v2"])

    num, denom = derive_path_price(
        [
            (h1_num, h1_denom, False),
            (h2_num, h2_denom, False),
            (h3_num, h3_denom, False),
        ]
    )
    derived = Fraction(num, denom)
    assert (
        derived
        == prices[
            "94dca24a1f1fcc2ff51cd90f32f4fe9e786d861a"
            + (
                "2dbf7d27598d26e883b8a0a2074061394fddfe9aebee6fab4924dd73ec1326162f453dc9"
            )
        ]
    )


def test_djed_pair_decodes():
    label, (num, denom) = parse_djed(_LEAVES["djed"])
    assert label == "USD"
    assert (num, denom) == (250000, 40719)
