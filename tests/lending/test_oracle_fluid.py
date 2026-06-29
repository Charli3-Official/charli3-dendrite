from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.fluid import FluidAggregated
from charli3_dendrite.lending.oracles.fluid import FluidAsset
from charli3_dendrite.lending.oracles.fluid import FluidCommonFeedData
from charli3_dendrite.lending.oracles.fluid import FluidPooled
from charli3_dendrite.lending.oracles.fluid import FluidResolver
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource


def _common():
    return FluidCommonFeedData(
        valid_from=100,
        valid_to=200,
        token=FluidAsset(policy_id=bytes.fromhex("aa" * 28), asset_name=b"TOK"),
    )


def test_aggregated_feed():
    agg = FluidAggregated(
        common=_common(),
        token_price_in_lovelaces=43,
        token_price_denominator=100_000,
    )
    ref = OracleRef(
        source=OracleSource.FLUID_AGGREGATED,
        token="tok",
        extra={"feed_cbor": agg.to_cbor_hex()},
    )
    price = FluidResolver(OracleSource.FLUID_AGGREGATED).resolve(
        ref, PoolStateList(root=[])
    )
    assert price.num == 43
    assert price.denom == 100_000
    assert price.valid_to == 200
    assert price.source is OracleSource.FLUID_AGGREGATED


def test_pooled_feed_uses_reserve_ratio():
    pooled = FluidPooled(
        common=_common(),
        token_a_amount_in_pool=1_000,
        token_b_amount_in_pool=5_000,
        pool_fees_per_mille=3,
    )
    ref = OracleRef(
        source=OracleSource.FLUID_POOLED,
        token="tok",
        extra={"feed_cbor": pooled.to_cbor_hex()},
    )
    price = FluidResolver(OracleSource.FLUID_POOLED).resolve(
        ref, PoolStateList(root=[])
    )
    # price of token_a in lovelace (token_b) = 5000 / 1000
    assert price.num == 5_000
    assert price.denom == 1_000


def test_non_hex_feed_cbor_returns_none():
    ref = OracleRef(
        source=OracleSource.FLUID_AGGREGATED,
        token="tok",
        extra={"feed_cbor": "zzzz"},
    )
    assert (
        FluidResolver(OracleSource.FLUID_AGGREGATED).resolve(
            ref, PoolStateList(root=[])
        )
        is None
    )


def test_garbage_hex_feed_cbor_returns_none():
    ref = OracleRef(
        source=OracleSource.FLUID_AGGREGATED,
        token="tok",
        extra={"feed_cbor": "deadbeef"},
    )
    assert (
        FluidResolver(OracleSource.FLUID_AGGREGATED).resolve(
            ref, PoolStateList(root=[])
        )
        is None
    )


def test_pooled_zero_token_a_returns_none():
    pooled = FluidPooled(
        common=_common(),
        token_a_amount_in_pool=0,
        token_b_amount_in_pool=5_000,
        pool_fees_per_mille=3,
    )
    ref = OracleRef(
        source=OracleSource.FLUID_POOLED,
        token="tok",
        extra={"feed_cbor": pooled.to_cbor_hex()},
    )
    assert (
        FluidResolver(OracleSource.FLUID_POOLED).resolve(ref, PoolStateList(root=[]))
        is None
    )
