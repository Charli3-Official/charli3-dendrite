"""Cardano-Swaps order book over one-way and two-way swaps.

A two-way swap UTxO quotes both directions of its pair, so the book lists it
once on each side: its ``TakeAsset1`` view on one side and its ``TakeAsset2``
view on the other. One-way and two-way levels merge into one book per side.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderBook
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderState
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeInt
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import CardanoSwapsTwoWayOrderState
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TakeAsset1
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TakeAsset2
from tests.test_cardanoswaps import _MAKER
from tests.test_cardanoswaps import _TAKER
from tests.test_cardanoswaps import _OfflineContext
from tests.test_cardanoswaps import _redeemers
from tests.test_cardanoswaps import _resting_state
from tests.test_cardanoswaps_two_way import _ada_pair
from tests.test_cardanoswaps_two_way import _state
from tests.test_cardanoswaps_two_way_contracts import USDM_ID
from tests.test_cardanoswaps_two_way_contracts import USDM_NAME
from tests.test_cardanoswaps_two_way_contracts import USDM_UNIT
from tests.test_cardanoswaps_two_way_contracts import vector_datum

# The book's pair: unit(0) = ADA, unit(1) = USDM. The sell side is ADA in,
# USDM out; the buy side is USDM in, ADA out.
PAIR = Assets(root={"lovelace": 0, USDM_UNIT: 0})


@pytest.fixture
def tx_builder() -> TransactionBuilder:
    return TransactionBuilder(_OfflineContext())


def _one_way(
    offer_ada: bool,
    num: int,
    den: int,
    offer_qty: int,
    tx_index: int,
) -> CardanoSwapsOrderState:
    """A one-way ADA/USDM swap at its own output index."""
    ada, usdm = (b"", b""), (USDM_ID, USDM_NAME)
    offer, ask = (ada, usdm) if offer_ada else (usdm, ada)
    state = _resting_state(*offer, *ask, num, den, offer_qty)
    state.tx_index = tx_index
    return state


def _levels(book_side) -> list[tuple[float, object]]:
    """Each level's price and state, in book order."""
    return [(level.price, level.state) for level in book_side]


def test_book_lists_both_directions_of_a_two_way_swap() -> None:
    book = CardanoSwapsOrderBook.get_book(PAIR, orders=[_ada_pair()])

    assert len(book.sell_book_full) == 1
    assert len(book.buy_book_full) == 1
    sell = book.sell_book_full[0]
    buy = book.buy_book_full[0]
    assert sell.state.take_asset2 is True
    assert (sell.state.in_unit, sell.state.out_unit) == ("lovelace", USDM_UNIT)
    assert sell.price == 1_900_000 / 3
    assert sell.quantity == 30
    assert buy.state.take_asset2 is False
    assert (buy.state.in_unit, buy.state.out_unit) == (USDM_UNIT, "lovelace")
    assert buy.price == 3 / 2_000_000
    assert buy.quantity == 10_000_000


def test_book_merges_one_way_and_two_way_swaps() -> None:
    two_way = _state(
        vector_datum(),
        {"lovelace": 10_000_000, USDM_UNIT: 30},
        tx_index=1,
    )
    cheap_sell = _one_way(False, 600_000, 1, 20, tx_index=2)
    dear_sell = _one_way(False, 700_000, 1, 20, tx_index=3)
    cheap_buy = _one_way(True, 1, 1_000_000, 5_000_000, tx_index=4)
    dear_buy = _one_way(True, 2, 1_000_000, 5_000_000, tx_index=5)

    book = CardanoSwapsOrderBook.get_book(
        PAIR,
        orders=[dear_sell, two_way, dear_buy, cheap_sell, cheap_buy],
    )

    sell = _levels(book.sell_book_full)
    assert [price for price, _ in sell] == [600_000, 1_900_000 / 3, 700_000]
    assert [state.tx_index for _, state in sell] == [2, 1, 3]
    buy = _levels(book.buy_book_full)
    assert [price for price, _ in buy] == [1 / 1_000_000, 3 / 2_000_000, 2 / 1_000_000]
    assert [state.tx_index for _, state in buy] == [4, 1, 5]


def test_book_dedupes_directions() -> None:
    state = _ada_pair()

    book = CardanoSwapsOrderBook.get_book(
        PAIR,
        orders=[state, state.opposite(), *state.directions()],
    )

    assert len(book.sell_book_full) == 1
    assert len(book.buy_book_full) == 1


def test_book_skips_a_drained_direction() -> None:
    # No USDM left: the USDM-out direction has nothing to give, the ADA-out
    # direction is still fillable.
    drained = _state(vector_datum(), {"lovelace": 10_000_000})

    book = CardanoSwapsOrderBook.get_book(PAIR, orders=[drained])

    assert len(book.sell_book_full) == 0
    assert len(book.buy_book_full) == 1
    assert book.buy_book_full[0].state.take_asset2 is False
    assert book.buy_book_full[0].quantity == 10_000_000


def test_book_skips_an_expired_two_way_swap() -> None:
    expired = _ada_pair(expiration=CardanoSwapsSomeInt(60_000))
    assert expired.inactive

    book = CardanoSwapsOrderBook.get_book(PAIR, orders=[expired])

    assert len(book.sell_book_full) == 0
    assert len(book.buy_book_full) == 0


@pytest.mark.parametrize(
    ("in_assets", "out_unit", "redeemer"),
    [
        (Assets(root={"lovelace": 1_900_000}), USDM_UNIT, TakeAsset2),
        (Assets(root={USDM_UNIT: 2}), "lovelace", TakeAsset1),
    ],
)
def test_book_fill_walks_into_a_two_way_swap(
    tx_builder,
    in_assets: Assets,
    out_unit: str,
    redeemer: type,
) -> None:
    state = _ada_pair()
    state.address = str(CardanoSwapsTwoWayOrderState.beacon_address(_MAKER))
    book = CardanoSwapsOrderBook.get_book(PAIR, orders=[state])
    out_assets, _ = book.get_amount_out(in_assets)
    assert out_assets.unit() == out_unit
    assert out_assets.quantity() > 0

    cont_txo, _ = book.swap_utxo(
        address_source=_TAKER,
        in_assets=in_assets,
        out_assets=out_assets,
        tx_builder=tx_builder,
    )

    assert cont_txo is not None
    assert cont_txo.address == CardanoSwapsTwoWayOrderState.beacon_address(_MAKER)
    assert tx_builder.outputs == [cont_txo]
    redeemers = _redeemers(tx_builder)
    assert len(redeemers) == 1
    assert isinstance(redeemers[0].data, redeemer)


class _Row(SimpleNamespace):
    """A backend row: ``model_dump`` returns the UTxO's values."""

    def model_dump(self) -> dict:
        return dict(self.values)


def test_book_discovers_one_way_and_two_way_swaps(monkeypatch) -> None:
    one_way = _one_way(False, 600_000, 1, 20, tx_index=2)
    one_way_values = one_way.model_dump()
    # A backend row carries no direction: it parses as the TakeAsset1 view.
    two_way_values = _ada_pair().model_dump()
    two_way_values.pop("take_asset2")
    rows = {
        CardanoSwapsOrderState.CONTRACT.beacon_policy_id: [_Row(values=one_way_values)],
        CardanoSwapsTwoWayOrderState.CONTRACT.beacon_policy_id: [
            _Row(values=two_way_values),
        ],
    }
    queried: list[list[str]] = []

    def get_pool_utxos(*, assets: list[str], **_kwargs) -> list[_Row]:
        queried.append(assets)
        return [row for policy in assets for row in rows[policy]]

    monkeypatch.setattr(
        "charli3_dendrite.dexs.ob.cardanoswaps.get_backend",
        lambda: SimpleNamespace(get_pool_utxos=get_pool_utxos),
    )

    book = CardanoSwapsOrderBook.get_book(PAIR)

    assert sorted(queried) == sorted([[policy] for policy in rows])
    sell = _levels(book.sell_book_full)
    assert [price for price, _ in sell] == [600_000, 1_900_000 / 3]
    assert isinstance(sell[0][1], CardanoSwapsOrderState)
    assert isinstance(sell[1][1], CardanoSwapsTwoWayOrderState)
    assert len(book.buy_book_full) == 1
    assert isinstance(book.buy_book_full[0].state, CardanoSwapsTwoWayOrderState)
