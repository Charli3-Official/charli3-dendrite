"""Unit tests for Danogo DEX module."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from charli3_dendrite.dexs.amm.danogo import ConcentratedPoolDatum
from charli3_dendrite.dexs.amm.danogo import ConcentratedPoolState
from charli3_dendrite.dexs.amm.danogo import PRational
from charli3_dendrite.dexs.amm.danogo import TupleAsset
from charli3_dendrite.dexs.amm.danogo import calculate_l
from charli3_dendrite.dexs.amm.danogo import calculate_xv_yv
from charli3_dendrite.utility import Assets

cbor_hex_input = (
    "d8799f9f4040ff9f581c9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5"
    "02456655534441ff0a000000d8799f1b000fe3624afc291b1b002386f26fc10000ffd8799f"
    "1a000f42401a000f4240ff1a022bfd431a00a673f21a829ccfa81a00010f91ff"
)


def _create_mock_pool_state(**overrides: dict) -> ConcentratedPoolState:  # type: ignore[type-arg]
    """Create a ConcentratedPoolState with minimal required fields for testing."""
    default_data = {
        "assets": Assets(root={"lovelace": 10_000_000}),
        "block_time": 1000000,
        "block_index": 1,
        "plutus_v2": True,
        "tx_index": 0,
        "tx_hash": "0" * 64,
        "datum_cbor": cbor_hex_input,
        "datum_hash": "0" * 64,
        "pool_nft": Assets(root={"test_nft": 1}),
    }
    default_data.update(overrides)

    # Use patch to bypass abstract method validation during construction
    with patch.object(ConcentratedPoolState, "__abstractmethods__", set()):
        return ConcentratedPoolState(**default_data)


def test_parse() -> None:
    """Test parsing of ConcentratedPoolDatum from CBOR."""
    # 1. Decode the hex string into the Datum object
    datum = ConcentratedPoolDatum.from_cbor(cbor_hex_input)

    # 2. Extract asset information using the TupleAsset helper
    asset_x = TupleAsset.from_list(datum.token_x)
    asset_y = TupleAsset.from_list(datum.token_y)

    # --- ASSERTIONS (Validation logic) ---

    # Check if Token X is correctly identified as ADA (lovelace)
    assert asset_x.unit == "lovelace", f"Expected lovelace, got {asset_x.unit}"

    # Check if Token Y matches the specific PolicyID + AssetName hex
    expected_y = "9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441"
    assert asset_y.unit == expected_y, "Token Y hex mismatch"

    # Check lp_fee_rate, platform_fee_x, platform_fee_y, and total_swap_fee
    lp_fee_rate_expected = 10
    assert datum.lp_fee_rate == lp_fee_rate_expected, "LP Fee Rate should be 10"
    assert datum.platform_fee_x == 0, "Platform Fee X should be 0"
    assert datum.platform_fee_y == 0, "Platform Fee Y should be 0"
    assert datum.total_swap_fee == 0, "Total Swap Fee should be 0"

    # Check sqrt_lower_price, sqrt_upper_price
    assert isinstance(datum.sqrt_lower_price, PRational)
    sqrt_lower_num = 4472135954999579
    sqrt_lower_den = 10000000000000000
    assert datum.sqrt_lower_price.numerator == sqrt_lower_num
    assert datum.sqrt_lower_price.denominator == sqrt_lower_den

    assert isinstance(datum.sqrt_upper_price, PRational)
    sqrt_upper_num = 1000000
    sqrt_upper_den = 1000000
    assert datum.sqrt_upper_price.numerator == sqrt_upper_num
    assert datum.sqrt_upper_price.denominator == sqrt_upper_den

    # Check min_x_change, min_y_change, circulating_lp_token, last_withdraw_epoch
    min_x_expected = 36437315
    min_y_expected = 10908658
    circulating_lp_expected = 2191314856
    last_withdraw_expected = 69521

    assert datum.min_x_change == min_x_expected, "Min X Change should be 36437315"
    assert datum.min_y_change == min_y_expected, "Min Y Change should be 10908658"
    assert (
        datum.circulating_lp_token == circulating_lp_expected
    ), "Circulating LP token should be 2191314856"
    assert (
        datum.last_withdraw_epoch == last_withdraw_expected
    ), "Last Withdraw Epoch should be 69521"

    # --- VISUAL FEEDBACK (Visible only with -s flag) ---
    print("\n" + "=" * 30)
    print(" DANOGO PARSE SUCCESSFUL ")
    print("=" * 30)
    print(f"Token X: {asset_x.unit}")
    print(f"Token Y: {asset_y.unit}")
    print(f"LP Fee Rate: {datum.lp_fee_rate}")
    print(f"Platform Fee X: {datum.platform_fee_x}")
    print(f"Platform Fee Y: {datum.platform_fee_y}")
    print(f"Total Swap Fee: {datum.total_swap_fee}")
    print(
        f"Sqrt Lower Price: "
        f"{datum.sqrt_lower_price.numerator}/{datum.sqrt_lower_price.denominator}",
    )
    print(
        f"Sqrt Upper Price: "
        f"{datum.sqrt_upper_price.numerator}/{datum.sqrt_upper_price.denominator}",
    )
    print(f"Min X Change: {datum.min_x_change}")
    print(f"Min Y Change: {datum.min_y_change}")
    print(f"Circulating LP Token: {datum.circulating_lp_token}")
    print(f"Last Withdraw Epoch: {datum.last_withdraw_epoch}")
    print("=" * 30)


def test_concentrated_pool_state_dex() -> None:
    """Test the dex identifier."""
    assert ConcentratedPoolState.dex() == "Danogo Concentrated Liquidity"


def test_concentrated_pool_state_pool_selector() -> None:
    """Test the pool selector configuration."""
    selector = ConcentratedPoolState.pool_selector()

    assert len(selector.addresses) == 1
    assert selector.addresses[0].startswith("addr_test1")
    assert len(selector.assets) == 1
    assert len(selector.assets[0]) > 0


def test_concentrated_pool_state_pool_datum_class() -> None:
    """Test that the correct datum class is returned."""
    assert ConcentratedPoolState.pool_datum_class() == ConcentratedPoolDatum


def test_concentrated_pool_state_pool_id() -> None:
    """Test pool_id property returns the NFT unit."""
    # Create a pool state with a specific NFT
    test_nft_unit = "test_policy_hex" + "test_asset_name_hex"
    pool_nft = Assets(root={test_nft_unit: 1})
    pool = _create_mock_pool_state(pool_nft=pool_nft)

    assert pool.pool_id == test_nft_unit


def test_concentrated_pool_state_price() -> None:
    """Test price calculation using virtual reserves."""
    # Create a mock pool state with ADA and USDM tokens
    unit_b = "9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441"
    assets = Assets(
        root={
            "lovelace": 100_000_000,  # 100 ADA (unit_a)
            unit_b: 50_000_000,  # 50 tokens (unit_b)
        },
    )

    # Mock asset_decimals to return 6 for both assets
    with patch("charli3_dendrite.dexs.amm.danogo.naturalize_assets") as mock_nat:
        mock_nat.return_value = {
            "lovelace": Decimal("100"),  # 100 ADA
            unit_b: Decimal("50"),  # 50 tokens
        }
        pool = _create_mock_pool_state(assets=assets, datum_cbor=cbor_hex_input)

        # Calculate prices
        price_b_in_a, price_a_in_b = pool.price

        # Prices should be positive decimals
        assert isinstance(price_b_in_a, Decimal)
        assert isinstance(price_a_in_b, Decimal)
        assert price_b_in_a > 0
        assert price_a_in_b > 0

        # Prices should be reciprocals (approximately)
        assert abs(price_b_in_a * price_a_in_b - Decimal(1)) < Decimal("0.0001")


def test_concentrated_pool_state_price_zero_reserves() -> None:
    """Test price calculation handles zero reserves gracefully."""
    unit_b = "9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441"
    assets = Assets(
        root={
            "lovelace": 0,
            unit_b: 0,
        },
    )

    # Mock naturalize_assets to return zero reserves
    with patch("charli3_dendrite.dexs.amm.danogo.naturalize_assets") as mock_nat:
        mock_nat.return_value = {
            "lovelace": Decimal("0"),
            unit_b: Decimal("0"),
        }
        pool = _create_mock_pool_state(assets=assets, datum_cbor=cbor_hex_input)

        price_b_in_a, price_a_in_b = pool.price

        # Should return (0, 0) for zero reserves
        assert price_b_in_a == Decimal(0)
        assert price_a_in_b == Decimal(0)


def test_concentrated_pool_state_tvl_ada_pair() -> None:
    """Test TVL calculation for ADA/Token pair."""
    unit_b = "9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441"
    assets = Assets(
        root={
            "lovelace": 100_000_000,  # 100 ADA
            unit_b: 50_000_000,
        },
    )

    # Mock naturalize_assets - returns lovelace (not ADA)
    with patch("charli3_dendrite.dexs.amm.danogo.naturalize_assets") as mock_nat:
        mock_nat.return_value = {
            "lovelace": Decimal("100000000"),  # 100M lovelace
            unit_b: Decimal("50"),
        }
        pool = _create_mock_pool_state(assets=assets, datum_cbor=cbor_hex_input)

        tvl = pool.tvl

        # TVL should be 200 ADA (100 * 2) for ADA pair
        assert tvl == Decimal(200)


def test_concentrated_pool_state_tvl_token_pair() -> None:
    """Test TVL calculation for Token/Token pair (no ADA).

    Note: This test is simplified because creating a proper token-token pool
    would require a matching datum. We test that the TVL method returns
    the correct format for such pools.
    """
    # Use the existing CBOR datum which has lovelace/USDM pair
    # The actual test just ensures the method works, not the specific logic
    unit_b = "9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441"
    assets = Assets(
        root={
            "lovelace": 5_000_000,  # 5 ADA min
            unit_b: 1000_000_000,
        },
    )

    # Mock naturalize_assets
    with patch("charli3_dendrite.dexs.amm.danogo.naturalize_assets") as mock_nat:
        mock_nat.return_value = {
            "lovelace": Decimal("5000000"),  # 5M lovelace
            unit_b: Decimal("1000"),
        }
        pool = _create_mock_pool_state(assets=assets, datum_cbor=cbor_hex_input)

        tvl = pool.tvl

        # TVL should be a positive Decimal
        assert isinstance(tvl, Decimal)
        assert tvl > 0


def test_calculate_l() -> None:
    """Test liquidity calculation."""
    x_r = Decimal(100_000_000)
    y_r = Decimal(50_000_000)
    sqrt_pa = Decimal("0.5")
    sqrt_pb = Decimal("2.0")

    l_value = calculate_l(x_r, y_r, sqrt_pa, sqrt_pb)

    # Liquidity should be positive
    assert l_value > 0
    assert isinstance(l_value, Decimal)


def test_calculate_xv_yv() -> None:
    """Test virtual reserves calculation."""
    x_r = 100_000_000
    y_r = 50_000_000
    sqrt_pa_num = 1000000
    sqrt_pa_den = 2000000  # 0.5
    sqrt_pb_num = 2000000
    sqrt_pb_den = 1000000  # 2.0

    xv, yv = calculate_xv_yv(
        x_r,
        y_r,
        sqrt_pa_num,
        sqrt_pa_den,
        sqrt_pb_num,
        sqrt_pb_den,
    )

    # Virtual reserves should be greater than or equal to real reserves
    assert xv >= Decimal(x_r)
    assert yv >= Decimal(y_r)
    assert isinstance(xv, Decimal)
    assert isinstance(yv, Decimal)


def test_calculate_xv_yv_zero_denominator() -> None:
    """Test that zero denominator raises ValueError."""
    with pytest.raises(ValueError, match="Denominators cannot be zero"):
        calculate_xv_yv(100, 50, 1, 0, 2, 1)
