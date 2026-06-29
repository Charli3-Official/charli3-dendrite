from decimal import Decimal

import pytest

from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.models import get_resolver
from charli3_dendrite.lending.oracles.models import register_resolver


def _price(token="tok", num=3, denom=2, vf=100, vt=200, src=OracleSource.CHARLI3):
    return OraclePrice(
        token=token,
        num=num,
        denom=denom,
        valid_from=vf,
        valid_to=vt,
        source=src,
    )


def test_as_decimal():
    assert _price(num=3, denom=2).as_decimal() == Decimal("1.5")


def test_is_fresh_window():
    p = _price(vf=100, vt=200)
    assert p.is_fresh(150)
    assert not p.is_fresh(99)
    assert not p.is_fresh(201)


def test_pricemap_keeps_freshest():
    pm = PriceMap()
    pm.add(_price(token="A", vt=100))
    pm.add(_price(token="A", vt=300))  # newer valid_to wins
    assert pm.get("A").valid_to == 300


def test_pricemap_require_missing_raises():
    pm = PriceMap()
    assert pm.get("missing") is None
    with pytest.raises(KeyError):
        pm.require("missing")


def test_oracleref_selector_from_feed_nft():
    ref = OracleRef(
        source=OracleSource.CHARLI3,
        token="tok",
        address="addr1xyz",
        feed_policy="aa",
        feed_name="bb",
    )
    sel = ref.selector()
    assert sel.addresses == ["addr1xyz"]
    assert sel.assets == ["aabb"]


def test_pricemap_none_valid_to_is_freshest():
    # An open-ended (None valid_to) price must not be overwritten by a dated one.
    pm = PriceMap()
    pm.add(_price(token="A", vt=None))
    pm.add(_price(token="A", vt=300))
    assert pm.get("A").valid_to is None
    # ...but an open-ended price does overwrite a dated one.
    pm2 = PriceMap()
    pm2.add(_price(token="B", vt=300))
    pm2.add(_price(token="B", vt=None))
    assert pm2.get("B").valid_to is None


def test_as_decimal_zero_denominator_raises():
    with pytest.raises(ZeroDivisionError):
        _price(num=1, denom=0).as_decimal()


def test_is_fresh_open_ended_bounds():
    no_lower = _price(vf=None, vt=200)
    assert no_lower.is_fresh(0)
    assert not no_lower.is_fresh(201)

    no_upper = _price(vf=100, vt=None)
    assert no_upper.is_fresh(10**18)
    assert not no_upper.is_fresh(99)

    unbounded = _price(vf=None, vt=None)
    assert unbounded.is_fresh(0)
    assert unbounded.is_fresh(10**18)


def test_oracleref_selector_none_when_nothing_to_select():
    ref = OracleRef(source=OracleSource.QTOKEN_RATE, token="tok")
    assert ref.selector() is None


def test_registry_roundtrip():
    class _Fake:
        source = OracleSource.DEX_POOLED

        def selectors(self, refs):
            return []

        def resolve(self, ref, utxos):
            return None

    register_resolver(_Fake())
    assert get_resolver(OracleSource.DEX_POOLED).source is OracleSource.DEX_POOLED
