from fractions import Fraction

import pytest

from charli3_dendrite.lending.danogo.oracles.leaves import cpamm_price_from_reserves
from charli3_dendrite.lending.danogo.oracles.leaves import danogo_pool_dtoken_rate
from charli3_dendrite.lending.danogo.oracles.leaves import liqwid_qtoken_rate
from charli3_dendrite.lending.danogo.oracles.leaves import parse_danogo_staking_rate
from charli3_dendrite.lending.danogo.oracles.leaves import parse_indigo
from charli3_dendrite.lending.danogo.oracles.leaves import parse_liqwid_market_param
from charli3_dendrite.lending.danogo.oracles.leaves import parse_liqwid_market_state


def test_liqwid_market_param_surfaces_base_rate_and_collateral_ratio():
    # tx daf74541...: interest-model/risk config record (not a price source).
    datum = (
        "9f9f011832ff1ab2d05e009f0a080000ff1a00e4e1c01a000493e0041a000dbba0048058"
        "1c4986257bffd6bc8bda0e56fd7796c5d125fe496deb3a00c5196068a31a001e84809f9f"
        "1b000000039a290d2f1b0011c37937e08000ff9f1b000000150e136c311b0011c37937e0"
        "8000ff9f1b000001fe2f977f3f1b0011c37937e08000ff9f090affff9f1921f8192710ff"
        "9f0101ff069f011904e2ff9f0102ff9f0102ff9f0101ff9f0101ff581c469772d2f93d70"
        "f92a4930fa608457392e58e480babf723ade7f9857581c6bb0851fa2bf40c6729c9d5df9"
        "7a4201fdea2b3ae169ad5242bfe47a581ca50ecf6e4d5621d034f0a09714da50cea8e94f"
        "cd77388954654d0479d8799f43d87980ff581c7fc0d659f74debe817faa6487f0cbc7d8c"
        "ec93997adaad543fbfff98d8799f43d87980ff80000000d87a80d87a80d87a80d87a80ff"
    )
    base_rate, collateral_ratio = parse_liqwid_market_param(datum)
    assert base_rate == (1, 50)
    assert collateral_ratio == (8696, 10000)


from charli3_dendrite.lending.danogo.oracles.leaves import parse_splash_cpamm_g3
from charli3_dendrite.lending.danogo.oracles.leaves import splash_lp_token_price


def test_liqwid_market_state_reproduces_qtoken_price():
    # tx 578fae97: exchange rate at state field 9; qToken price in ADA =
    # rate / 100 / ada_usd, with ada_usd = 3133/12500 from the sibling ADA oracle.
    datum = (
        "9f1b0000c6cb9e88acc23b0000006b69a1596b1b002ac04c770c72de1b00007e99a6b34045"
        "1b0000029cb31fec631b0071afe871471c8d9fc24b079f0deb4837f450cddc4fc24d0b614b"
        "2766ee12c4adf7ba8000ff1b0000019cce6fb7d01b0000019cce83dc109f1b0001477c3aef"
        "10891b002ac04c770c72deff1a002dc6c0ff"
    )
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


def test_splash_g3_datum_surfaces_pool_assets():
    # USDA/BTC pool: asset_x=USDA, asset_y=BTC, lp=USDA_BTC_LQ.
    datum = (
        "d8799fd8799f581c112d79f5387b1876f92158d00c182bb6d527a75e4e90cb615c21a02f"
        "4c555344415f4254435f4e4654ffd8799f581cfe7c786ab321f41c654ef6c1af7b3250a6"
        "13c24e4213e0425a7ae4564455534441ffd8799f581c25c5de5f5b286073c593edfd77b4"
        "8abc7a48e5a4f3d4cd9d428ff93543425443ffd8799f581c33e085ecce54025b6635af11"
        "f5bbe15ea329734fc7209db9482a930a4b555344415f4254435f4c51ff1a00018376185a"
        "18641a05c0c9591a002cbd101a06646e131a0031b5be9fd87981d87a81581c66e711a4bf"
        "9ddf46ff239143870b6893055a4fd4dea9f99fed6665cdff581c75c4570eb625ae881b32"
        "a34c52b159f6f3f3f2c7aaabf5bac46881335820102a9f40831eec20979a38d3fe4b77cc"
        "ad3ba22baab3704fc2d9eb56487b4b1600ff"
    )
    ax, ay, lp = parse_splash_cpamm_g3(datum)
    assert ay == "25c5de5f5b286073c593edfd77b48abc7a48e5a4f3d4cd9d428ff935425443"
    assert lp.endswith("555344415f4254435f4c51")


def test_splash_lp_token_price_matches_onchain():
    # tx dd724653...d2f4d37: LP token in BTC = 2*6193503/(2**63-1 - 9223372036854735307)
    # = 12387006/40500 = 76463/250 (the exact on-chain redeemer price).
    num, denom = splash_lp_token_price(
        reserve_quote=6193503, lp_balance=9223372036854735307
    )
    assert (num, denom) == (12387006, 40500)
    assert Fraction(num, denom) == Fraction(76463, 250)


def test_splash_lp_token_price_full_pool_rejected():
    # No circulating LP (pool holds the entire cap) -> undefined.
    with pytest.raises(ValueError):
        splash_lp_token_price(reserve_quote=10, lp_balance=(1 << 63) - 1)


def test_danogo_staking_rate_matches_onchain_redeemer():
    # tx ad5ec9af...5ec862, leaf (OUT, TDANOGO_STAKING, 1): datum
    # [15013620167, 14750722364, ts]; the redeemer priced the staked token at
    # exactly 2144802881/2107246052 (= 15013620167/14750722364 reduced).
    datum = "d8799f1b000000037ee1a9c71b000000036f36293c1b0000019cd9b5b838ff"
    num, denom = parse_danogo_staking_rate(datum)
    assert (num, denom) == (15013620167, 14750722364)
    assert Fraction(num, denom) == Fraction(2144802881, 2107246052)


def test_danogo_staking_rate_zero_supply_rejected():
    # Constr0[0, 1, ts] -> non-positive total.
    with pytest.raises(ValueError):
        parse_danogo_staking_rate("d8799f000101ff")


def test_indigo_price_scale_is_1e6():
    # Indigo datum Constr0[Constr0[4330793], ts] -> 4330793/1_000_000 ADA per iAsset.
    datum = "d8799fd8799f1a00421529ff1b0000019e6e014ad8ff"
    assert parse_indigo(datum) == (4330793, 1_000_000)


def test_indigo_zero_price_rejected():
    with pytest.raises(ValueError):
        parse_indigo("d8799fd8799f00ff1b0000019e6e014ad8ff")


def test_danogo_pool_dtoken_rate_matches_onchain_redeemer():
    # tx 590d1cf6...e6a069, leaf (OUT, TDANOGO_POOL, 0): the pool output carried
    # total_supply=557273970678 / circulating_dtoken=562160462680, and the
    # redeemer's price for that collateral was exactly 278636985339/281080231340.
    num, denom = danogo_pool_dtoken_rate(
        total_supply=557273970678, circulating_dtoken=562160462680
    )
    assert Fraction(num, denom) == Fraction(278636985339, 281080231340)


def test_danogo_pool_dtoken_rate_zero_supply_rejected():
    with pytest.raises(ValueError):
        danogo_pool_dtoken_rate(total_supply=0, circulating_dtoken=1)
    with pytest.raises(ValueError):
        danogo_pool_dtoken_rate(total_supply=1, circulating_dtoken=0)


def test_cpamm_spot_price_from_reserves():
    # reserve_quote=2_000_000, reserve_token=1_000_000 -> price token->quote = 2/1
    assert cpamm_price_from_reserves(
        reserve_token=1_000_000, reserve_quote=2_000_000
    ) == (2_000_000, 1_000_000)


def test_cpamm_zero_reserve_rejected():
    with pytest.raises(ValueError):
        cpamm_price_from_reserves(reserve_token=0, reserve_quote=2_000_000)


def test_liqwid_qtoken_rate_passthrough():
    assert liqwid_qtoken_rate(
        qtoken_rate_num=1_050_000, qtoken_rate_denom=1_000_000
    ) == (1_050_000, 1_000_000)


def test_liqwid_qtoken_zero_denom_rejected():
    with pytest.raises(ValueError):
        liqwid_qtoken_rate(qtoken_rate_num=1, qtoken_rate_denom=0)
