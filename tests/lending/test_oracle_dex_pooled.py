from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.dex_pooled import DexPooledResolver
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource

TOK = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddd"


def _pool_info():
    return PoolStateInfo(
        address="addr_pool",
        tx_hash="ab",
        tx_index=0,
        block_time=1,
        block_index=1,
        block_hash="cd",
        datum_hash="00",
        datum_cbor="d87980",
        assets=Assets(root={"lovelace": 5_000, TOK: 1_000}),
        plutus_v2=True,
    )


def test_dex_pooled_price_from_reserves():
    ref = OracleRef(
        source=OracleSource.DEX_POOLED,
        token=TOK,
        quote="lovelace",
        address="addr_pool",
        extra={"reserve_token": TOK, "reserve_quote": "lovelace"},
    )
    price = DexPooledResolver().resolve(ref, PoolStateList(root=[_pool_info()]))
    assert price.num == 5_000  # quote reserve
    assert price.denom == 1_000  # token reserve
    assert price.source is OracleSource.DEX_POOLED


def test_dex_pooled_no_pool_returns_none():
    ref = OracleRef(
        source=OracleSource.DEX_POOLED,
        token=TOK,
        quote="lovelace",
        address="addr_pool",
        extra={"reserve_token": TOK, "reserve_quote": "lovelace"},
    )
    assert DexPooledResolver().resolve(ref, PoolStateList(root=[])) is None


def _ref():
    return OracleRef(
        source=OracleSource.DEX_POOLED,
        token=TOK,
        quote="lovelace",
        address="addr_pool",
        extra={"reserve_token": TOK, "reserve_quote": "lovelace"},
    )


def _info(assets, address="addr_pool"):
    return PoolStateInfo(
        address=address,
        tx_hash="ab",
        tx_index=0,
        block_time=1,
        block_index=1,
        block_hash="cd",
        datum_hash="00",
        datum_cbor="d87980",
        assets=Assets(root=assets),
        plutus_v2=True,
    )


def test_dex_pooled_zero_token_reserve_skipped():
    info = _info({"lovelace": 5_000, TOK: 0})
    assert DexPooledResolver().resolve(_ref(), PoolStateList(root=[info])) is None


def test_dex_pooled_zero_quote_reserve_skipped():
    info = _info({"lovelace": 0, TOK: 1_000})
    assert DexPooledResolver().resolve(_ref(), PoolStateList(root=[info])) is None


def test_dex_pooled_missing_reserve_unit_returns_none():
    info = _info({"lovelace": 5_000})
    assert DexPooledResolver().resolve(_ref(), PoolStateList(root=[info])) is None


def test_dex_pooled_address_mismatch_returns_none():
    info = _info({"lovelace": 5_000, TOK: 1_000}, address="addr_other")
    assert DexPooledResolver().resolve(_ref(), PoolStateList(root=[info])) is None
