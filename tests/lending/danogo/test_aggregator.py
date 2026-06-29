import pytest

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.danogo.oracles.aggregator import DanogoAggregatorResolver
from charli3_dendrite.lending.danogo.oracles.aggregator import average_paths
from charli3_dendrite.lending.danogo.oracles.aggregator import calc_out_amount
from charli3_dendrite.lending.danogo.oracles.aggregator import derive_path_price
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource


def test_calc_out_amount_forward_and_reverse():
    assert calc_out_amount(in_amount=100, num=3, denom=2, is_reverse=False) == 150
    assert calc_out_amount(in_amount=150, num=3, denom=2, is_reverse=True) == 100


def test_calc_out_amount_zero_denominator_rejected():
    with pytest.raises(ValueError):
        calc_out_amount(in_amount=100, num=3, denom=0, is_reverse=False)
    with pytest.raises(ValueError):
        calc_out_amount(in_amount=100, num=0, denom=2, is_reverse=True)


def test_derive_path_price_chains_hops():
    # A->X at 2/1, X->B at 3/1  => A->B = 6/1
    price = derive_path_price([(2, 1, False), (3, 1, False)])
    assert price[0] / price[1] == pytest.approx(6.0)


def test_average_paths_rejects_outlier():
    # two paths ~1.00, one outlier 2.00; deviation 5% -> outlier dropped
    num, denom = average_paths([(100, 100), (101, 100), (200, 100)], deviation_bps=500)
    assert 0.99 <= num / denom <= 1.02


def test_derive_path_price_reverse_hop():
    assert derive_path_price([(2, 1, True)]) == (1, 2)


def test_derive_path_price_empty_raises():
    with pytest.raises(ValueError):
        derive_path_price([])


def test_average_paths_single_path():
    num, denom = average_paths([(5, 2)], deviation_bps=500)
    assert num / denom == pytest.approx(2.5)


def test_average_paths_two_paths_no_rejection():
    # 2 paths -> plain average (no meaningful rejection possible)
    num, denom = average_paths([(100, 100), (200, 100)], deviation_bps=500)
    assert num / denom == pytest.approx(1.5)


def test_average_paths_empty_raises():
    with pytest.raises(ValueError):
        average_paths([], deviation_bps=0)


def test_resolver_resolve_happy():
    ref = OracleRef(
        source=OracleSource.DANOGO_AGGREGATOR,
        token="aa",
        quote="lovelace",
        extra={"paths": [[(2, 1, False)]], "deviation_bps": 0},
    )
    price = DanogoAggregatorResolver().resolve(ref, PoolStateList(root=[]))
    assert price is not None
    assert price.num / price.denom == pytest.approx(2.0)
    assert price.source == OracleSource.DANOGO_AGGREGATOR


def test_resolver_resolve_no_paths_returns_none():
    ref = OracleRef(source=OracleSource.DANOGO_AGGREGATOR, token="aa", extra={})
    assert DanogoAggregatorResolver().resolve(ref, PoolStateList(root=[])) is None


def test_resolver_resolve_malformed_deviation_does_not_raise():
    ref = OracleRef(
        source=OracleSource.DANOGO_AGGREGATOR,
        token="aa",
        extra={"paths": [[(2, 1, False)]], "deviation_bps": "abc"},
    )
    price = DanogoAggregatorResolver().resolve(ref, PoolStateList(root=[]))
    assert price is not None
    assert price.num / price.denom == pytest.approx(2.0)


def test_resolver_resolve_skips_malformed_path():
    ref = OracleRef(
        source=OracleSource.DANOGO_AGGREGATOR,
        token="aa",
        extra={"paths": [[(2, 1)], [(3, 1, False)]], "deviation_bps": 0},
    )
    price = DanogoAggregatorResolver().resolve(ref, PoolStateList(root=[]))
    assert price is not None
    assert price.num / price.denom == pytest.approx(3.0)


def test_resolver_selectors_skips_malformed():
    ref = OracleRef(
        source=OracleSource.DANOGO_AGGREGATOR,
        token="aa",
        extra={"selectors": [{"addresses": ["addr1xyz"], "assets": None}, "notadict"]},
    )
    selectors = DanogoAggregatorResolver().selectors([ref])
    assert len(selectors) == 1
    assert isinstance(selectors[0], PoolSelector)
