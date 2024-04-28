from decimal import Decimal

from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import BaseList
from cardex.dataclasses.models import CardexBaseModel
from cardex.dexs.core.base import AbstractPairState
from cardex.utility import Assets
from pycardano import UTxO
from pydantic import model_validator


class OrderBookOrder(CardexBaseModel):
    price: float
    quantity: int
    address: str | None = None
    tx_hash: str | None = None
    tx_index: int | None = None
    datum: str | None = None


class BuyOrderBook(BaseList):
    root: list[OrderBookOrder]

    @model_validator(mode="after")
    def sort_descend(v: list[OrderBookOrder]):
        return sorted(v, key=lambda x: x.price)


class SellOrderBook(BaseList):
    root: list[OrderBookOrder]

    @model_validator(mode="after")
    def sort_descend(v: list[OrderBookOrder]):
        return sorted(v, key=lambda x: x.price)


class AbstractOrderBookState(AbstractPairState):
    sell_book: SellOrderBook
    buy_book: BuyOrderBook
    sell_book_full: SellOrderBook
    buy_book_full: BuyOrderBook

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the amount of token output for the given input.

        Args:
            asset: The input assets
            precise: If precise, uses integers. Defaults to True.

        Returns:
            tuple[Assets, float]: The output assets and slippage.
        """
        assert len(asset) == 1, "Asset should only have one token."
        assert asset.unit() in [
            self.unit_a,
            self.unit_b,
        ], f"Asset {asset.unit} is invalid for pool {self.unit_a}-{self.unit_b}"

        if asset.unit() == self.unit_a:
            book = self.sell_book_full
            unit_out = self.unit_b
        else:
            book = self.buy_book_full
            unit_out = self.unit_a

        # Calculate adjustment based on fees
        asset.root[asset.unit()] = (
            (10000 - self.volume_fee) * asset[asset.unit()] // 10000
        )

        index = 0
        in_quantity = asset.quantity()
        out_assets = Assets({unit_out: 0})
        while in_quantity > 0 and index < len(book):
            available = book[index].quantity * book[index].price
            if available > in_quantity:
                out_assets.root[unit_out] += in_quantity / book[index].price
                in_quantity = 0
            else:
                out_assets.root[unit_out] += book[index].quantity
                in_quantity -= book[index].price * book[index].quantity
            index += 1

        out_assets.root[unit_out] = int(out_assets[unit_out])

        return out_assets, 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the amount of token input for the given output.

        Args:
            asset: The input assets
            precise: If precise, uses integers. Defaults to True.

        Returns:
            tuple[Assets, float]: The output assets and slippage.
        """
        assert len(asset) == 1, "Asset should only have one token."
        assert asset.unit() in [
            self.unit_a,
            self.unit_b,
        ], f"Asset {asset.unit} is invalid for pool {self.unit_a}-{self.unit_b}"

        if asset.unit() == self.unit_b:
            book = self.sell_book_full
            unit_in = self.unit_a
        else:
            book = self.buy_book_full
            unit_in = self.unit_b

        index = 0
        out_quantity = asset.quantity()
        in_assets = Assets({unit_in: 0})
        while out_quantity > 0 and index < len(book):
            available = book[index].quantity
            if available > out_quantity:
                in_assets.root[unit_in] += out_quantity * book[index].price
                out_quantity = 0
            else:
                in_assets.root[unit_in] += book[index].quantity / book[index].price
                out_quantity -= book[index].quantity
            index += 1

        in_assets.root[unit_in] = int(in_assets[unit_in])

        return in_assets, 0

    @classmethod
    @property
    def reference_utxo(self) -> UTxO | None:
        return None

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Mid price of assets.

        Returns:
            A `Tuple[Decimal, Decimal] where the first `Decimal` is the price to buy
                1 of token B in units of token A, and the second `Decimal` is the price
                to buy 1 of token A in units of token B.
        """
        prices = (
            Decimal((self.buy_book[0].price + 1 / self.sell_book[0].price) / 2),
            Decimal((self.sell_book[0].price + 1 / self.buy_book[0].price) / 2),
        )

        return prices

    @property
    def tvl(self) -> Decimal:
        """Return the total value locked for the pool.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        if self.unit_a != "lovelace":
            raise NotImplementedError("tvl for non-ADA pools is not implemented.")

        tvl = sum(b.quantity / b.price for b in self.buy_book) + sum(
            s.quantity * s.price for s in self.sell_book
        )

        return Decimal(int(tvl) / 10**6)
