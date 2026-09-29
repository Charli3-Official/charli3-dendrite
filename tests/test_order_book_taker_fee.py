"""Order-book taker fees: where SaturnSwap takes its fee, and the book accessor.

SaturnSwap pays its taker fee out of the sell asset an order releases. An order
releases at most its sell balance, so a taker can keep at most that balance less
the fee on it, however much input it offers.

Each order book also reports the taker fee its level walk charges a fill.
"""

from pathlib import Path

import pytest
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderBook
from charli3_dendrite.dexs.ob.chadswap import ChadSwapOrderBook
from charli3_dendrite.dexs.ob.geniusyield import GeniusYieldOrderBook
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.dexs.ob.ob_base import fill_within_budget
from charli3_dendrite.dexs.ob.saturnswap import SATURNSWAP_AUTHORIZE_KEY_ENV
from charli3_dendrite.dexs.ob.saturnswap import SATURNSWAP_LEGACY_TAKER_FEE_BPS
from charli3_dendrite.dexs.ob.saturnswap import SATURNSWAP_TAKER_FEE_BPS
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapLegacyOrderState
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapOrderBook
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapOutputReference
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapSwapDatum
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapSwapDatumV3
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapTxId
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapV3OrderState
from charli3_dendrite.dexs.ob.saturnswap import _ratio_amount
from pycardano import PaymentSigningKey

_FIX = Path(__file__).parent / "fixtures" / "saturnswap_v3"
# Sells 1,500 tokens for 3,000,000 lovelace.
_SMALL = "order_ad182bcd.hex"
# Sells 3,000 tokens for 6,000,000 lovelace (same price, twice the size).
_LARGE = "order_b6bcaeb6_out2_cov_none.hex"
_BPS = 10_000


@pytest.fixture(autouse=True)
def _no_authorize_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price every order with its on-chain taker fee unless a test opts out."""
    monkeypatch.delenv(SATURNSWAP_AUTHORIZE_KEY_ENV, raising=False)


def _v3_datum(name: str) -> SaturnSwapSwapDatumV3:
    return SaturnSwapSwapDatumV3.from_cbor((_FIX / name).read_text().strip())


def _state(datum_cbor: str, datum, state_cls, tx_index: int):  # noqa: ANN001, ANN202
    sell_unit = datum.policy_id_sell.hex() + datum.asset_name_sell.hex()
    return state_cls(
        tx_hash="ab" * 32,
        tx_index=tx_index,
        datum_cbor=datum_cbor,
        datum_hash="cd" * 32,
        assets=Assets(**{"lovelace": 0, sell_unit: int(datum.amount_sell)}),
        blockTime=0,
        blockIndex=tx_index,
        plutusV2=False,
    )


def _v3_state(name: str, tx_index: int = 0) -> SaturnSwapV3OrderState:
    """A 1% order on the V3 contract."""
    datum = _v3_datum(name)
    return _state(datum.to_cbor_hex(), datum, SaturnSwapV3OrderState, tx_index)


def _legacy_state(name: str, tx_index: int = 0) -> SaturnSwapLegacyOrderState:
    """The same order's terms, resting on the legacy 4% contract."""
    v3 = _v3_datum(name)
    datum = SaturnSwapSwapDatum(
        owner=v3.owner,
        policy_id_sell=v3.policy_id_sell,
        asset_name_sell=v3.asset_name_sell,
        amount_sell=v3.amount_sell,
        policy_id_buy=v3.policy_id_buy,
        asset_name_buy=v3.asset_name_buy,
        amount_buy=v3.amount_buy,
        valid_before_time=PlutusNone(),
        output_reference=SaturnSwapOutputReference(
            tx_id=SaturnSwapTxId(value=v3.output_reference.tx_id),
            index=v3.output_reference.index,
        ),
    )
    return _state(datum.to_cbor_hex(), datum, SaturnSwapLegacyOrderState, tx_index)


def _kept(gross: int, fee_bps: int) -> int:
    """Sell asset a taker keeps when ``gross`` is released and the fee paid from it."""
    return gross - gross * fee_bps // _BPS


def _sell(state, quantity: int) -> Assets:  # noqa: ANN001
    return Assets(**{state.out_unit: quantity})


# --- SaturnSwap: the fee comes out of what the order releases ------------------


@pytest.mark.parametrize(
    ("make_state", "fee_bps"),
    [
        (_v3_state, SATURNSWAP_TAKER_FEE_BPS),
        (_legacy_state, SATURNSWAP_LEGACY_TAKER_FEE_BPS),
    ],
)
@pytest.mark.parametrize("multiple", [1, 2, 10])
def test_a_full_fill_keeps_the_balance_less_the_fee(
    make_state,  # noqa: ANN001
    fee_bps: int,
    multiple: int,
) -> None:
    """Offering the whole ask, or more, keeps ``amount_sell`` less its fee."""
    state = make_state(_SMALL)
    amount_sell = int(state.order_datum.amount_sell)
    budget = Assets(lovelace=int(state.order_datum.amount_buy) * multiple)

    out, _ = state.get_amount_out(budget)

    assert out.quantity() == _kept(amount_sell, fee_bps)


@pytest.mark.parametrize("make_state", [_v3_state, _legacy_state])
def test_asking_for_the_whole_balance_costs_the_whole_ask(
    make_state,  # noqa: ANN001
) -> None:
    """An order never takes more than ``amount_buy``, even asked for everything."""
    state = make_state(_SMALL)

    need, _ = state.get_amount_in(_sell(state, int(state.order_datum.amount_sell)))

    assert need.quantity() == int(state.order_datum.amount_buy)


@pytest.mark.parametrize("make_state", [_v3_state, _legacy_state])
@pytest.mark.parametrize(
    "budget",
    [1_000_000, 2_999_999, 3_000_000, 3_000_001, 4_567_891, 6_000_000, 30_000_000],
)
def test_a_quoted_fill_is_one_the_order_can_settle(
    make_state,  # noqa: ANN001
    budget: int,
) -> None:
    """The fill spends at most the ask and is quoted at most what the taker keeps.

    The order releases ``_ratio_amount(amount_buy, spent, amount_sell)`` of its
    sell asset and pays the fee out of that, as the fill it builds does.
    """
    state = make_state(_SMALL)
    amount_buy = int(state.order_datum.amount_buy)
    amount_sell = int(state.order_datum.amount_sell)

    out, spent = fill_within_budget(state, Assets(lovelace=budget))
    released = _ratio_amount(amount_buy, spent.quantity(), amount_sell)

    assert spent.quantity() <= min(budget, amount_buy)
    assert released <= amount_sell
    assert out.quantity() <= _kept(released, state.volume_fee)


def test_an_authorized_fill_keeps_the_whole_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fill co-signed by the authorize key pays no fee, so nothing is carved."""
    monkeypatch.setenv(
        SATURNSWAP_AUTHORIZE_KEY_ENV,
        PaymentSigningKey.generate().to_cbor_hex(),
    )
    state = _v3_state(_SMALL)

    out, _ = state.get_amount_out(
        Assets(lovelace=2 * int(state.order_datum.amount_buy)),
    )

    assert out.quantity() == int(state.order_datum.amount_sell)


def test_the_book_prices_each_order_net_of_its_fee() -> None:
    """Draining the book delivers each balance less its fee, for the sum of asks.

    Priced from either end: every order's net balance costs the sum of the asks,
    and the sum of the asks buys every order's net balance.
    """
    orders = [_v3_state(_SMALL, 0), _v3_state(_LARGE, 1)]
    book = SaturnSwapOrderBook.get_book(orders[0].assets, orders=orders)
    deliverable = sum(
        _kept(int(o.order_datum.amount_sell), SATURNSWAP_TAKER_FEE_BPS) for o in orders
    )
    total_ask = sum(int(o.order_datum.amount_buy) for o in orders)

    need, _ = book.get_amount_in(_sell(orders[0], deliverable))
    out, _ = book.get_amount_out(Assets(lovelace=total_ask))

    assert need.quantity() == total_ask
    assert out.quantity() == deliverable


# --- The fee an order book's level walk charges --------------------------------


def _saturnswap_book(*states) -> SaturnSwapOrderBook:  # noqa: ANN002
    return SaturnSwapOrderBook.get_book(states[0].assets, orders=list(states))


def test_saturnswap_book_reports_its_contract_fee() -> None:
    """A book of 1% orders charges 1%."""
    book = _saturnswap_book(_v3_state(_SMALL, 0), _v3_state(_LARGE, 1))

    assert book.taker_fee_bps == SATURNSWAP_TAKER_FEE_BPS


def test_saturnswap_book_reports_the_largest_fee_when_contracts_mix() -> None:
    """A book holding a legacy 4% order alongside 1% orders reports 4%."""
    book = _saturnswap_book(_v3_state(_SMALL, 0), _legacy_state(_LARGE, 1))

    assert book.taker_fee_bps == SATURNSWAP_LEGACY_TAKER_FEE_BPS


def test_saturnswap_book_reports_no_fee_on_authorized_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the authorize key configured every fill is fee-free."""
    monkeypatch.setenv(
        SATURNSWAP_AUTHORIZE_KEY_ENV,
        PaymentSigningKey.generate().to_cbor_hex(),
    )
    book = _saturnswap_book(_v3_state(_SMALL, 0), _legacy_state(_LARGE, 1))

    assert book.taker_fee_bps == 0


def test_an_empty_saturnswap_book_charges_nothing() -> None:
    """With no orders to fill, the walk charges no fee."""
    small = _v3_state(_SMALL)
    book = SaturnSwapOrderBook.get_book(small.assets, orders=[])

    assert book.taker_fee_bps == 0


_TOKEN = "ab" * 28 + "746f6b656e"


def _level_book(book_cls, price: float, quantity: int):  # noqa: ANN001, ANN202
    return book_cls(
        assets=Assets(**{"lovelace": 0, _TOKEN: 0}),
        plutus_v2=False,
        block_time=0,
        block_index=0,
        sell_book_full=SellOrderBook(
            [OrderBookOrder(price=price, quantity=quantity)],
        ),
        buy_book_full=BuyOrderBook([]),
    )


def test_geniusyield_book_reports_the_fee_its_walk_charges() -> None:
    """GeniusYield's walk takes its per-order taker fee off the input."""
    price, budget = 2.0, 1_000_000
    book = _level_book(GeniusYieldOrderBook, price, 10_000_000)

    out, _ = book.get_amount_out(Assets(lovelace=budget))

    assert book.taker_fee_bps == 30  # noqa: PLR2004
    assert out.quantity() == int(budget * (_BPS - book.taker_fee_bps) // _BPS / price)


@pytest.mark.parametrize("book_cls", [CardanoSwapsOrderBook, ChadSwapOrderBook])
def test_books_without_a_taker_fee_report_none(
    book_cls,  # noqa: ANN001
) -> None:
    """CardanoSwaps has no protocol fee and ChadSwap's is paid by the maker."""
    book = _level_book(book_cls, 2.0, 10_000_000)

    assert book.taker_fee_bps == 0
