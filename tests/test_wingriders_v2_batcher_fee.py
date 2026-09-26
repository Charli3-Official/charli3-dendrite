"""WingRiders V2 batcher fee: a size-tiered fee kept out of a flat 4 ADA attachment."""

import pytest
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.wingriders import WingRidersV2CPPState
from charli3_dendrite.dexs.amm.wingriders import WingRidersV2OrderDatum
from charli3_dendrite.dexs.amm.wingriders import WingRidersV2SSPState
from pycardano import Address

TOKEN = "f13ac4d66b3ee19a6aa0f2a22298737bd907cc95121662fc971b5275535452494b45"
OTHER = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d"
SOURCE = Address.from_primitive(
    "addr1qy6fgvsmep5yxpdtyfsyn26dddswns92qqg0ysv8snkc3j5ej7za8s8pvffkjd9m4jfkwrcanendk8qvrjp80cz08y7qfxa5hd",
)
TARGET = Address.from_primitive(
    "addr1q9ld26v2lv8wvrxxmvg90pn8n8n5k6tdst06q2s856rwmvnueldzuuqmnsye359fqrk8hwvenjnqultn7djtrlft7jnq7dy7wv",
)
FOUR_ADA = 4_000_000
TOP_TIER_FEE = 2_000_000
TIERS = [
    (1_000_000, 850_000),
    (250_000_000, 850_000),
    (250_000_001, 1_500_000),
    (500_000_000, 1_500_000),
    (500_000_001, 2_000_000),
    (1_000_000_000, 2_000_000),
]
POOL_CLASSES = [WingRidersV2CPPState, WingRidersV2SSPState]


@pytest.mark.parametrize("cls", POOL_CLASSES)
@pytest.mark.parametrize(("lovelace", "fee"), TIERS)
def test_buy_fee_is_tiered_by_the_order_ada(cls: type, lovelace: int, fee: int) -> None:
    """ADA -> token: the fee steps down with the ADA the order moves."""
    pool = cls.model_construct()
    in_assets, out_assets = Assets(lovelace=lovelace), Assets(root={TOKEN: 1})
    assert (
        pool.batcher_fee(in_assets=in_assets, out_assets=out_assets).quantity() == fee
    )


@pytest.mark.parametrize("cls", POOL_CLASSES)
@pytest.mark.parametrize(("lovelace", "fee"), TIERS)
def test_sell_fee_is_tiered_by_the_order_ada(
    cls: type,
    lovelace: int,
    fee: int,
) -> None:
    """Token -> ADA: the ADA received sets the tier."""
    pool = cls.model_construct()
    in_assets, out_assets = Assets(root={TOKEN: 1}), Assets(lovelace=lovelace)
    assert (
        pool.batcher_fee(in_assets=in_assets, out_assets=out_assets).quantity() == fee
    )


@pytest.mark.parametrize("lovelace", [lovelace for lovelace, _ in TIERS])
def test_fee_plus_refundable_deposit_is_always_four_ada(lovelace: int) -> None:
    """Every order attaches 4 ADA: the kept fee plus the refunded deposit."""
    pool = WingRidersV2CPPState.model_construct()
    in_assets, out_assets = Assets(lovelace=lovelace), Assets(root={TOKEN: 1})
    fee = pool.batcher_fee(in_assets=in_assets, out_assets=out_assets).quantity()
    deposit = pool.deposit(in_assets=in_assets, out_assets=out_assets).quantity()
    assert fee + deposit == FOUR_ADA


def test_token_to_token_order_pays_the_top_tier() -> None:
    """With no ADA swapped there is no tier to apply."""
    pool = WingRidersV2CPPState.model_construct()
    in_assets, out_assets = Assets(root={TOKEN: 1}), Assets(root={OTHER: 1})
    fee = pool.batcher_fee(in_assets=in_assets, out_assets=out_assets).quantity()
    assert fee == TOP_TIER_FEE


@pytest.mark.parametrize(("lovelace", "fee"), TIERS)
def test_forwarded_order_passes_on_what_the_batcher_does_not_keep(
    lovelace: int,
    fee: int,
) -> None:
    """A forwarded order's oil is the attachment minus the fee the batcher keeps."""
    pool = WingRidersV2CPPState.model_construct()
    in_assets, out_assets = Assets(lovelace=lovelace), Assets(root={TOKEN: 1})
    datum = WingRidersV2OrderDatum.create_datum(
        address_source=SOURCE,
        in_assets=in_assets,
        out_assets=out_assets,
        batcher_fee=pool.batcher_fee(in_assets=in_assets, out_assets=out_assets),
        deposit=pool.deposit(in_assets=in_assets, out_assets=out_assets),
        address_target=TARGET,
    )
    assert datum.oil == FOUR_ADA - fee


def test_fee_without_order_assets_is_the_top_tier() -> None:
    """Called without the order's assets, the fee is the full 2 ADA."""
    pool = WingRidersV2CPPState.model_construct()
    assert pool.batcher_fee().quantity() == TOP_TIER_FEE
