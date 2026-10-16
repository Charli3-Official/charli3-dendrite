"""Liqwid datum parsers against real mainnet CBOR (``fixtures/mainnet_datums.json``)."""

import json
from fractions import Fraction
from pathlib import Path

import pytest

from charli3_dendrite.lending.liqwid.datums import liqwid_oracle_feed_id
from charli3_dendrite.lending.liqwid.datums import liqwid_qtoken_rate
from charli3_dendrite.lending.liqwid.datums import parse_liqwid_market_param
from charli3_dendrite.lending.liqwid.datums import parse_liqwid_market_state
from charli3_dendrite.lending.liqwid.datums import parse_liqwid_oracle_v2

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "mainnet_datums.json").read_text()
)


def test_liqwid_oracle_v2_parses_priced_asset_and_rate():
    # ADA feed: field 1 is the empty TupleAsset, field 2 the [num, denom] rate, and
    # field 0 the oracle updater's key.
    datum = FIX["oracle_v2"]["datum"]
    assert parse_liqwid_oracle_v2(datum) == ("lovelace", (16271, 100000))
    assert (
        liqwid_oracle_feed_id(datum)
        == "49afdaa6689e72e3f96ddac9872fad0ee2503fc3268a8868c17eee1f"
    )


def test_liqwid_market_param_surfaces_initial_qtoken_rate_and_max_ltv():
    # Interest-model / risk config record (not a price source).
    datum = FIX["market_param"]["datum"]
    initial_qtoken_rate, max_ltv = parse_liqwid_market_param(datum)
    assert initial_qtoken_rate == (1, 50)
    assert max_ltv == (8696, 10000)


def test_liqwid_market_state_reproduces_qtoken_price():
    # Exchange rate at state field 9; qToken price in ADA = rate / 100 / ada_usd,
    # with ada_usd = 3133/12500 from the sibling ADA oracle.
    datum = FIX["market_state"]["datum"]
    num, denom = parse_liqwid_market_state(datum)
    assert (num, denom) == (360073866973321, 12033383669199582)
    ada_usd = Fraction(3133, 12500)
    qtoken_price = Fraction(num, denom) / 100 / ada_usd
    assert qtoken_price == Fraction(45009233371665125, 37700591035602290406)


def test_liqwid_market_state_zero_rate_rejected():
    # 10-element array: ints 0..8 then a zero rational [0,1] at index 9.
    zero = "9f0001020304050607089f0001ffff"
    with pytest.raises(ValueError):
        parse_liqwid_market_state(zero)


def test_liqwid_qtoken_rate_passthrough():
    assert liqwid_qtoken_rate(
        qtoken_rate_num=1_050_000, qtoken_rate_denom=1_000_000
    ) == (1_050_000, 1_000_000)


def test_liqwid_qtoken_zero_denom_rejected():
    with pytest.raises(ValueError):
        liqwid_qtoken_rate(qtoken_rate_num=1, qtoken_rate_denom=0)
