"""An order-book taker fill never spends more than its input budget.

Each order's pricing rounds the output up and the input for that output up again, so
the input a partially-filled last order asks for can exceed what is left of the budget.
The book must shrink that order's fill until its input fits.
"""

from pathlib import Path

import pytest
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapOrderBook
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapSwapDatumV3
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapV3OrderState

_FIX = Path(__file__).parent / "fixtures" / "saturnswap_v3"
# 1,500 tokens for 3,000,000 lovelace, and 3,000 tokens for 6,000,000 lovelace.
_ORDERS = ["order_ad182bcd.hex", "order_b6bcaeb6_out2_cov_none.hex"]


def _order_state(name: str, tx_index: int) -> SaturnSwapV3OrderState:
    cbor = (_FIX / name).read_text().strip()
    datum = SaturnSwapSwapDatumV3.from_cbor(cbor)
    sell_unit = datum.policy_id_sell.hex() + datum.asset_name_sell.hex()
    return SaturnSwapV3OrderState(
        tx_hash="ab" * 32,
        tx_index=tx_index,
        datum_cbor=cbor,
        datum_hash="cd" * 32,
        assets=Assets(**{"lovelace": 0, sell_unit: int(datum.amount_sell)}),
        blockTime=0,
        blockIndex=tx_index,
        plutusV2=False,
    )


def _book() -> SaturnSwapOrderBook:
    orders = [_order_state(name, i) for i, name in enumerate(_ORDERS)]
    return SaturnSwapOrderBook.get_book(orders[0].assets, orders=orders)


def _fills(monkeypatch: pytest.MonkeyPatch, budget: int) -> list[tuple[int, int]]:
    """Walk the book's taker fill for ``budget`` lovelace; record each order's (in, out)."""
    fills: list[tuple[int, int]] = []

    def record(self, *, in_assets, out_assets, **_kwargs):  # noqa: ANN001, ANN202
        fills.append((in_assets.quantity(), out_assets.quantity()))
        return None, None

    monkeypatch.setattr(SaturnSwapV3OrderState, "swap_utxo", record)
    _book().swap_utxo(
        address_source=None,
        in_assets=Assets(lovelace=budget),
        out_assets=Assets(lovelace=0),
        tx_builder=object(),
    )
    return fills


@pytest.mark.parametrize("budget", [2_001, 3_000_001, 3_002_001, 4_567_891])
def test_taker_fill_spends_at_most_its_budget(
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
) -> None:
    fills = _fills(monkeypatch, budget)
    assert sum(spent for spent, _ in fills) <= budget


@pytest.mark.parametrize("budget", [2_001, 3_000_001, 3_002_001, 4_567_891])
def test_quote_matches_what_the_fill_delivers(
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
) -> None:
    quoted, _ = _book().get_amount_out(Assets(lovelace=budget))
    delivered = sum(out for _, out in _fills(monkeypatch, budget))
    assert quoted.quantity() == delivered


@pytest.mark.parametrize("budget", [2_001, 3_000_001, 3_002_001, 4_567_891])
def test_partial_fill_is_the_largest_that_fits(budget: int) -> None:
    from charli3_dendrite.dexs.ob.ob_base import fill_within_budget

    state = _order_state(_ORDERS[1], 1)
    out, spent = fill_within_budget(state, Assets(lovelace=budget))
    assert spent.quantity() <= budget
    one_more, _ = state.get_amount_in(Assets(**{out.unit(): out.quantity() + 1}))
    assert one_more.quantity() > budget
