"""Borrow terms: the validity window signed prices allow, and the collateral floor."""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from math import ceil
from pathlib import Path

import cbor2
import pytest
from pycardano import RawPlutusData

from charli3_dendrite.lending.fluidtokens.datums import Asset
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    BORROW_VALIDITY_SLOTS,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    borrow_window,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_collateral_amount,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    signed_window,
)
from charli3_dendrite.utility import slot_to_posix_ms

_SLOT = 198_000_000
_POOL = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "entities.json").read_text(),
)["pool"]
_TOKEN = Asset(policy_id=b"\x01" * 28, asset_name=b"TOKEN")
# Lovelace per smallest unit of the principal and of the collateral.
_PRINCIPAL_PRICE = (386_452_445, 100_000_000)
_COLLATERAL_PRICE = (10_186_901, 100_000_000)


def _reward(first_slot: int, last_slot: int) -> OracleReward:
    """A signed price whose window spans ``first_slot`` .. ``last_slot``."""
    return OracleReward(
        valid_from_ms=slot_to_posix_ms(first_slot),
        valid_to_ms=slot_to_posix_ms(last_slot),
        collateral_policy="",
        collateral_name="",
        price_num=1,
        price_den=1,
        signature=b"",
        cbor="",
    )


def test_window_starts_at_the_tip_inside_every_price() -> None:
    early = _reward(_SLOT - 100, _SLOT + 3000)
    late = _reward(_SLOT - 50, _SLOT + 2000)
    assert signed_window(
        [early, late],
        valid_from=None,
        valid_to=None,
        tip=_SLOT,
        cap=BORROW_VALIDITY_SLOTS,
    ) == (_SLOT, _SLOT + 2000)


def test_window_without_prices_is_capped_from_the_tip() -> None:
    assert signed_window([], valid_from=None, valid_to=None, tip=_SLOT, cap=600) == (
        _SLOT,
        _SLOT + 600,
    )


def test_a_pinned_window_must_fit_every_price() -> None:
    inside = _reward(_SLOT - 10, _SLOT + 100)
    assert signed_window(
        [inside],
        valid_from=_SLOT,
        valid_to=_SLOT + 100,
        tip=0,
        cap=BORROW_VALIDITY_SLOTS,
    ) == (_SLOT, _SLOT + 100)
    with pytest.raises(ValueError, match="not covered"):
        signed_window(
            [inside, _reward(_SLOT + 1, _SLOT + 100)],
            valid_from=_SLOT,
            valid_to=_SLOT + 100,
            tip=0,
            cap=BORROW_VALIDITY_SLOTS,
        )
    with pytest.raises(ValueError, match="exceeds"):
        signed_window(
            [],
            valid_from=_SLOT,
            valid_to=_SLOT + BORROW_VALIDITY_SLOTS + 1,
            tip=0,
            cap=BORROW_VALIDITY_SLOTS,
        )


def test_a_price_that_expired_before_the_tip_raises() -> None:
    with pytest.raises(ValueError, match="stale"):
        signed_window(
            [_reward(_SLOT - 500, _SLOT - 1)],
            valid_from=None,
            valid_to=None,
            tip=_SLOT,
            cap=BORROW_VALIDITY_SLOTS,
        )


def test_borrow_window_is_the_one_price_case() -> None:
    reward = _reward(_SLOT - 100, _SLOT + 5000)
    assert borrow_window(reward, valid_from=None, valid_to=None, tip=_SLOT) == (
        _SLOT,
        _SLOT + BORROW_VALIDITY_SLOTS,
    )


def _pool(*, principal: Asset | None = None, dynamic: bool = True) -> PoolDatum:
    """The captured pool, its principal and price mode optionally replaced."""
    datum = PoolDatum.from_cbor(_POOL["datum_cbor"])
    if principal is not None:
        datum = replace(
            datum,
            common_data=replace(datum.common_data, principal_asset=principal),
        )
    if not dynamic:
        datum = replace(
            datum,
            dynamic_collateral_price=RawPlutusData(cbor2.CBORTag(121, [])),  # False
        )
    return datum


def _floor(datum: PoolDatum, principal: Fraction, collateral: Fraction) -> int:
    """principal x its price / LTV / the collateral price, rounded up (index 0)."""
    ltv = Fraction(datum.min_collateral[0], datum.min_collateral_divider[0])
    return ceil(1_000_003 * principal / ltv / collateral)


def test_a_token_principal_is_priced_by_its_own_oracle() -> None:
    datum = _pool(principal=_TOKEN)
    assert min_collateral_amount(
        datum,
        chosen_collateral_index=0,
        principal_amount=1_000_003,
        price_num=_COLLATERAL_PRICE[0],
        price_den=_COLLATERAL_PRICE[1],
        principal_price_num=_PRINCIPAL_PRICE[0],
        principal_price_den=_PRINCIPAL_PRICE[1],
    ) == _floor(datum, Fraction(*_PRINCIPAL_PRICE), Fraction(*_COLLATERAL_PRICE))


def test_a_token_principal_without_its_price_is_refused() -> None:
    with pytest.raises(NotImplementedError, match="principal oracle price"):
        min_collateral_amount(
            _pool(principal=_TOKEN),
            chosen_collateral_index=0,
            principal_amount=1_000_003,
            price_num=_COLLATERAL_PRICE[0],
            price_den=_COLLATERAL_PRICE[1],
        )


def test_an_ada_principal_ignores_a_principal_price() -> None:
    datum = _pool()
    kwargs = {
        "chosen_collateral_index": 0,
        "principal_amount": 1_000_003,
        "price_num": _COLLATERAL_PRICE[0],
        "price_den": _COLLATERAL_PRICE[1],
    }
    expected = _floor(datum, Fraction(1), Fraction(*_COLLATERAL_PRICE))
    assert min_collateral_amount(datum, **kwargs) == expected
    assert (
        min_collateral_amount(
            datum,
            principal_price_num=_PRINCIPAL_PRICE[0],
            principal_price_den=_PRINCIPAL_PRICE[1],
            **kwargs,
        )
        == expected
    )


def test_a_statically_priced_pool_ignores_both_prices() -> None:
    datum = _pool(principal=_TOKEN, dynamic=False)
    assert min_collateral_amount(
        datum,
        chosen_collateral_index=0,
        principal_amount=1_000_003,
        price_num=_COLLATERAL_PRICE[0],
        price_den=_COLLATERAL_PRICE[1],
        principal_price_num=_PRINCIPAL_PRICE[0],
        principal_price_den=_PRINCIPAL_PRICE[1],
    ) == ceil(
        Fraction(1_000_003 * datum.min_collateral[0], datum.min_collateral_divider[0]),
    )
