from decimal import Decimal

from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.qtoken import QTokenResolver


def test_qtoken_rate_from_extra():
    ref = OracleRef(
        source=OracleSource.QTOKEN_RATE,
        token="qADA",
        quote="lovelace",
        extra={"num": 1_050_000, "denom": 1_000_000, "valid_from": 1, "valid_to": 9},
    )
    price = QTokenResolver().resolve(ref, PoolStateList(root=[]))
    assert price.as_decimal() == Decimal("1.05")
    assert price.valid_from == 1
    assert price.valid_to == 9
    assert price.source is OracleSource.QTOKEN_RATE


def test_qtoken_missing_rate_returns_none():
    ref = OracleRef(source=OracleSource.QTOKEN_RATE, token="qADA")
    assert QTokenResolver().resolve(ref, PoolStateList(root=[])) is None


def test_qtoken_non_positive_rate_returns_none():
    for extra in (
        {"num": 1_050_000, "denom": 0},
        {"num": 1_050_000, "denom": -1},
        {"num": 0, "denom": 1_000_000},
        {"num": -5, "denom": 1_000_000},
    ):
        ref = OracleRef(source=OracleSource.QTOKEN_RATE, token="qADA", extra=extra)
        assert QTokenResolver().resolve(ref, PoolStateList(root=[])) is None


def test_qtoken_non_numeric_rate_returns_none():
    ref = OracleRef(
        source=OracleSource.QTOKEN_RATE,
        token="qADA",
        extra={"num": "abc", "denom": 1_000_000},
    )
    assert QTokenResolver().resolve(ref, PoolStateList(root=[])) is None
