"""The validity window a set of signed oracle prices allows."""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    BORROW_VALIDITY_SLOTS,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    borrow_window,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    signed_window,
)
from charli3_dendrite.utility import slot_to_posix_ms

_SLOT = 198_000_000


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
