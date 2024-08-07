"""This module defines base classes and utilities for order book handling in DEX."""
from abc import abstractmethod
from decimal import Decimal
from math import ceil
from typing import Any

from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import UTxO
from pydantic import model_validator

from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import BaseList
from cardex.dataclasses.models import CardexBaseModel
from cardex.dexs.core.base import AbstractPairState
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError

ASSET_COUNT_ONE = 1
ASSET_COUNT_TWO = 2
ASSET_COUNT_THREE = 3


class AbstractOrderState(AbstractPairState):
    """This class is largely used for OB dexes that allow direct script inputs."""

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False

    _batcher_fee: Assets
    _datum_parsed: PlutusData | None = None

    @property
    def in_unit(self) -> str:
        """Returns input assets unit."""
        return self.assets.unit()

    @property
    def out_unit(self) -> str:
        """Returns output assets unit."""
        return self.assets.unit(1)

    @property
    @abstractmethod
    def price(self) -> tuple[Decimal, Decimal]:
        """Returns the price."""
        raise NotImplementedError

    @property
    @abstractmethod
    def available(self) -> Assets:
        """Max amount of output asset that can be used to fill the order."""
        raise NotImplementedError

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Calculate the output amount for a specific limit order in an order book.

        Args:
            asset (Assets): asset expected to contain exactly one unit type.
            precise (bool): If True: the output rounded to the nearest integer.

        Returns:
            tuple[Assets, float]: Output assets and a float, always 0 in this context.
        """
        if not (asset.unit() == self.in_unit and len(asset) == 1):
            msg = "The asset must match the input unit and contain exactly one value."
            raise ValueError(msg)

        num, denom = self.price
        out_assets = Assets(**{self.out_unit: 0})

        volume_fee: int = 0
        if isinstance(self.volume_fee, int):
            volume_fee = self.volume_fee

        in_quantity = asset.quantity() - ceil(
            asset.quantity() * (volume_fee) / 10000,
        )
        out_assets.root[self.out_unit] = min(
            ceil(in_quantity * denom / num),
            self.available.quantity(),
        )

        if precise:
            out_assets.root[self.out_unit] = int(out_assets.quantity())

        return out_assets, 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Calculate the input amount for a specific limit order in an order book.

        Args:
            asset (Assets): expected to contain exactly one unit type.
            precise (bool): If True, input quantity rounded to the nearest integer.

        Returns:
            tuple[Assets, float]: Input assets and a float, always 0 in this context.
        """
        if not (asset.unit() == self.out_unit and len(asset) == 1):
            msg = (
                "The asset unit must match the out unit and contain exactly one value."
            )
            raise ValueError(msg)

        denom, num = self.price
        in_assets = Assets(**{self.in_unit: 0})
        out_quantity = asset.quantity()
        in_assets.root[self.in_unit] = int(
            (min(out_quantity, self.available.quantity()) * denom) / num
        )
        fees = in_assets[self.in_unit] * self.volume_fee / 10000
        in_assets.root[self.in_unit] += fees

        if precise:
            in_assets.root[self.in_unit] = int(in_assets.quantity())

        return in_assets, 0

    @classmethod
    def skip_init(cls, values: dict[str, Any]) -> bool:  # noqa: ARG003
        """An initial check to determine if parsing should be carried out.

        Args:
            values: The pool initialization parameters.

        Returns:
            bool: If this returns True, initialization checks will get skipped.
        """
        return False

    @classmethod
    def extract_dex_nft(cls, values: dict[str, Any]) -> Assets | None:
        """Extract the dex nft from the UTXO.

        Some DEXs put a DEX nft into the pool UTXO.

        This function checks to see if the DEX nft is in the UTXO if the DEX policy is
        defined.

        If the dex nft is in the values, this value is skipped because it is assumed
        that this utxo has already been parsed.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the dex nft.
        """
        assets = values["assets"]
        dex_policy = cls.dex_policy()
        # If no dex policy id defined, return nothing
        if dex_policy is None:
            dex_nft = None

        # If the dex nft is in the values, it's been parsed already
        elif "dex_nft" in values:
            if not any(
                any(p.startswith(d) for d in dex_policy) for p in values["dex_nft"]
            ):
                msg = "Invalid DEX NFT"
                raise NotAPoolError(msg)
            dex_nft = values["dex_nft"]

        # Check for the dex nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in dex_policy)
            ]
            if len(nfts) < 1:
                msg = f"{cls.__name__}: Pool must have one DEX NFT token."
                raise NotAPoolError(
                    msg,
                )
            dex_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["dex_nft"] = dex_nft

        return dex_nft

    @property
    def order_datum(self) -> PlutusData:
        """Retrieve and parse the order datum if not already parsed.

        Returns:
            PlutusData: The parsed order datum.
        """
        if self._datum_parsed is None:
            self._datum_parsed = self.order_datum_class().from_cbor(self.datum_cbor)
        return self._datum_parsed

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Post initialization checks.

        Args:
            values: The pool initialization parameters
        """
        assets = values["assets"]
        non_ada_assets = [a for a in assets if a != "lovelace"]

        # ADA pair
        if len(assets) == ASSET_COUNT_TWO and len(non_ada_assets) != ASSET_COUNT_ONE:
            msg = f"Pool must only have 1 non-ADA asset: {values}"
            raise ValueError(msg)

        # Non-ADA pair
        if len(assets) == ASSET_COUNT_THREE:
            if len(non_ada_assets) != ASSET_COUNT_TWO:
                msg = "Pool must only have 2 non-ADA assets."
                raise ValueError(msg)
            # Send the ADA token to the end
            values["assets"].root["lovelace"] = values["assets"].root.pop("lovelace")

        elif len(assets) == ASSET_COUNT_ONE and "lovelace" in assets:
            msg = f"Invalid pool, only contains lovelace: assets={assets}"
            raise NoAssetsError(
                msg,
            )
        else:
            msg = (
                f"Pool must have 2 or 3 assets except factor, NFT, "
                f"and LP tokens: assets={assets}"
            )
            raise InvalidPoolError(
                msg,
            )
        return values

    @classmethod
    @model_validator(mode="before")
    def translate_address(cls, values: dict[str, Any]) -> dict[str, Any]:
        """The main validation function called when initialized.

        Args:
            values: The pool initialization values.

        Returns:
            The parsed/modified pool initialization values.
        """
        if "assets" in values:
            if values["assets"] is None:
                msg = "No assets in the pool."
                raise NoAssetsError(msg)
            if not isinstance(values["assets"], Assets):
                values["assets"] = Assets(**values["assets"])

        if cls.skip_init(values):
            return values

        # Parse the order datum
        try:
            datum = cls.order_datum_class().from_cbor(values["datum_cbor"])
        except (DeserializeException, TypeError) as e:
            raise NotAPoolError(
                "Order datum could not be deserialized: \n "
                + f"    error={e}\n"
                + f"    tx_hash={values['tx_hash']}\n"
                + f"    datum={values['datum_cbor']}\n",
            ) from e

        # To help prevent edge cases, remove pool tokens while running other checks
        pair = datum.pool_pair()
        if datum.pool_pair() is not None:
            for token in datum.pool_pair():
                try:
                    if token in values["assets"]:
                        pair.root.update({token: values["assets"].root.pop(token)})
                except KeyError:
                    raise InvalidPoolError(
                        "Order does not contain expected asset.\n"
                        + f"    Expected: {token}\n"
                        + f"    Actual: {values['assets']}",
                    ) from KeyError

        _ = cls.extract_dex_nft(values)

        # Add the pool tokens back in
        values["assets"].root.update(pair.root)

        cls.post_init(values)

        return values


class OrderBookOrder(CardexBaseModel):
    """Represents an order in the order book."""

    price: float
    quantity: int
    state: AbstractOrderState | None = None


class BuyOrderBook(BaseList):
    """Represents a buy order book with sorted orders."""

    root: list[OrderBookOrder]

    @model_validator(mode="after")
    def sort_descend(self) -> list[OrderBookOrder]:
        """Sort orders in descending order by price."""
        self.root.sort(key=lambda x: x.price)
        return self


class SellOrderBook(BaseList):
    """Represents a sell order book with sorted orders."""

    root: list[OrderBookOrder]

    @model_validator(mode="after")
    def sort_descend(self) -> list[OrderBookOrder]:
        """Sort orders in descending order by price."""
        self.root.sort(key=lambda x: x.price)
        return self


class AbstractOrderBookState(AbstractPairState):
    """This class is largely used for OB dexes that have a batcher."""

    sell_book: SellOrderBook | None = None
    buy_book: BuyOrderBook | None = None
    sell_book_full: SellOrderBook
    buy_book_full: BuyOrderBook

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = False,
    ) -> tuple[Assets, float]:
        """Get the amount of token output for the given input.

        Args:
            asset: The input assets
            precise: If precise, uses integers. Defaults to True.
            apply_fee: If True, applies transaction fees. Defaults to False.

        Returns:
            tuple[Assets, float]: The output assets and slippage.
        """
        if len(asset) != 1:
            msg = "Asset should only have one token."
            raise ValueError(msg)
        if asset.unit() not in [self.unit_a, self.unit_b]:
            msg = (
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}"
            )
            raise ValueError(msg)

        if asset.unit() == self.unit_a:
            book = self.sell_book_full
            unit_out = self.unit_b
        else:
            book = self.buy_book_full
            unit_out = self.unit_a

        in_quantity = asset.quantity()
        fee: int = 0
        if isinstance(self.fee, int):
            fee = self.fee
        if apply_fee:
            in_quantity = in_quantity * (10000 - fee) // 10000

        index = 0
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
        apply_fee: bool = False,
    ) -> tuple[Assets, float]:
        """Get the amount of token input for the given output.

        Args:
            asset: The input assets
            precise: If precise, uses integers. Defaults to True.
            apply_fee: If True, applies transaction fees. Defaults to False.

        Returns:
            tuple[Assets, float]: The output assets and slippage.
        """
        if len(asset) != ASSET_COUNT_ONE:
            msg = "Asset should only have one token."
            raise ValueError(msg)
        if asset.unit() not in [self.unit_a, self.unit_b]:
            msg = (
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}"
            )
            raise ValueError(msg)

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

        if apply_fee:
            fees = in_assets[unit_in] * self.fee / 10000
            in_assets.root[unit_in] += fees

        in_assets.root[unit_in] = int(in_assets[unit_in])

        return in_assets, 0

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Returns reference utxo."""
        return None

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Mid price of assets.

        Returns:
            A `Tuple[Decimal, Decimal] where the first `Decimal` is the price to buy
                1 of token B in units of token A, and the second `Decimal` is the price
                to buy 1 of token A in units of token B.
        """
        if (
            self.buy_book is None
            or self.sell_book is None
            or len(self.buy_book) == 0
            or len(self.sell_book) == 0
        ):
            msg = "Buy book or sell book is not initialized or empty."
            raise ValueError(msg)
        return (
            Decimal((self.buy_book[0].price + 1 / self.sell_book[0].price) / 2),
            Decimal((self.sell_book[0].price + 1 / self.buy_book[0].price) / 2),
        )

    @property
    def tvl(self) -> Decimal:
        """Return the total value locked for the pool.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        if self.unit_a != "lovelace":
            msg = "tvl for non-ADA pools is not implemented."
            raise NotImplementedError(msg)

        tvl = Decimal(0)

        if self.buy_book is not None:
            tvl += sum(b.quantity / b.price for b in self.buy_book)
        if self.sell_book is not None:
            tvl += sum(s.quantity * s.price for s in self.sell_book)

        return Decimal(int(tvl) / 10**6)

    @classmethod
    @abstractmethod
    def get_book(
        cls,
        assets: Assets | None = None,
        orders: list[AbstractOrderState] | None = None,
    ) -> "AbstractOrderBookState":
        """Abstract method to retrieve an order book state.

        Args:
            assets: Optional. assets associated with the order book. Defaults to None.
            orders: Optional. list to initialize the order book. Defaults to None.

        Returns:
            AbstractOrderBookState: An instance of an abstract order book state.

        Raises:
            NotImplementedError: If the method is not implemented in the subclass.
        """
        raise NotImplementedError
