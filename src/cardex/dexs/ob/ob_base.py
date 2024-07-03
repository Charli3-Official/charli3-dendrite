"""Base classes & utility functions for managing order books in the DEX."""

from abc import abstractmethod
from decimal import Decimal
from typing import Any

from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import UTxO
from pydantic import model_validator

from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import BaseList
from cardex.dataclasses.models import CardexBaseModel
from cardex.dexs.core.base import AbstractPairState
from cardex.dexs.core.constants import ONE_VALUE
from cardex.dexs.core.constants import THREE_VALUE
from cardex.dexs.core.constants import TWO_VALUE
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError


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
        """Returns assets in unit."""
        return self.assets.unit()

    @property
    def out_unit(self) -> str:
        """Returns assets out unit."""
        return self.assets.unit(1)

    @property
    @abstractmethod
    def price(self) -> tuple[Decimal, Decimal]:
        """Returns the price. Method not implemented."""
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
        """Get the amount of token output for the given input.

        Args:
            asset: The input assets
            precise: If precise, uses integers. Defaults to True.

        Returns:
            tuple[Assets, float]: The output assets and slippage.
        """
        if not (asset.unit() == self.in_unit and len(asset) == ONE_VALUE):
            error_msg = "The asset unit must match the input unit and contain exactly one value."
            raise ValueError(error_msg)

        num, denom = self.price
        out_assets = Assets(**{self.out_unit: 0})

        fee = self.fee if self.fee is not None else 0
        in_quantity = asset.quantity() * (10000 - fee) // 10000

        available_quantity = int(self.available.quantity())
        calculated_amount = int((in_quantity * denom) // num)
        out_assets.root[self.out_unit] = min(calculated_amount, available_quantity)

        if precise:
            out_assets.root[self.out_unit] = int(out_assets[self.out_unit])

        return out_assets, 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Calculates the amount in and slippage for given output asset.

        Args:
            asset (Assets): The output asset.
            precise (bool, optional): Whether to calculate precisely. Defaults to True.

        Returns:
            tuple[Assets, float]: The amount in and slippage.
        """
        if not (asset.unit() == self.out_unit and len(asset) == ONE_VALUE):
            error_msg = (
                "The asset unit must match the out unit and contain exactly one value."
            )
            raise ValueError(error_msg)

        denom, num = self.price
        in_assets = Assets(**{self.in_unit: 0})
        out_quantity = asset.quantity()
        in_assets.root[self.in_unit] = int(
            (min(out_quantity, self.available.quantity()) * denom) / num,
        )
        fees = in_assets[self.in_unit] * self.fee / 10000
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

        # If no dex policy id defined, return nothing
        dex_policy = cls.dex_policy()
        if dex_policy is None:
            return None

        # If the dex nft is in the values, it's been parsed already
        if "dex_nft" in values and values["dex_nft"] is not None:
            if not any(
                any(p.startswith(d) for d in dex_policy) for p in values["dex_nft"]
            ):
                error_msg = "Invalid DEX NFT"
                raise NotAPoolError(error_msg)
            dex_nft = values["dex_nft"]

        # Check for the dex nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in dex_policy)
            ]
            if len(nfts) < ONE_VALUE:
                error_msg = f"{cls.__name__}: Pool must have one DEX NFT token."
                raise NotAPoolError(error_msg)
            dex_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["dex_nft"] = dex_nft

        return dex_nft

    @property
    def order_datum(self) -> PlutusData:
        """Retrieve and parse the order datum if not already parsed.

        Returns:
            PlutusData: The parsed order datum.

        Raises:
            ValueError: If the order datum is not valid.
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
        if len(assets) == TWO_VALUE and len(non_ada_assets) != ONE_VALUE:
            error_msg = f"Pool must only have 1 non-ADA asset: {values}"
            raise ValueError(error_msg)
        # Non-ADA pair
        if len(assets) == THREE_VALUE:
            if len(non_ada_assets) != TWO_VALUE:
                error_msg = "Pool must only have 2 non-ADA assets."
                raise ValueError(error_msg)
            # Send the ADA token to the end
            values["assets"].root["lovelace"] = values["assets"].root.pop("lovelace")

        elif len(assets) == ONE_VALUE and "lovelace" in assets:
            error_msg = f"Invalid pool, only contains lovelace: assets={assets}"
            raise NoAssetsError(
                error_msg,
            )
        else:
            error_msg = f"Pool must have 2 or 3 assets except factor, NFT, and LP tokens: assets={assets}"
            raise InvalidPoolError(
                error_msg,
            )
        return values

    @model_validator(mode="before")
    def translate_address(self, values: dict[str, Any]) -> dict[str, Any]:
        """The main validation function called when initialized.

        Args:
            values: The pool initialization values.

        Returns:
            The parsed/modified pool initialization values.
        """
        if "assets" in values:
            if values["assets"] is None:
                error_msg = "No assets in the pool."
                raise NoAssetsError(error_msg)
            if not isinstance(values["assets"], Assets):
                values["assets"] = Assets(**values["assets"])

        if self.skip_init(values):
            return values

        # Parse the order datum
        try:
            datum = self.order_datum_class().from_cbor(values["datum_cbor"])
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
                except KeyError as err:
                    raise InvalidPoolError(
                        "Order does not contain expected asset.\n"
                        + f"    Expected: {token}\n"
                        + f"    Actual: {values['assets']}",
                    ) from err

        _ = self.extract_dex_nft(values)

        # Add the pool tokens back in
        values["assets"].root.update(pair.root)

        self.post_init(values)

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
    def sort_descend(self, v: list[OrderBookOrder]) -> list[OrderBookOrder]:
        """Sort orders in descending order by price."""
        return sorted(v, key=lambda x: x.price)


class SellOrderBook(BaseList):
    """Represents a sell order book with sorted orders."""

    root: list[OrderBookOrder]

    @model_validator(mode="after")
    def sort_descend(self, v: list[OrderBookOrder]) -> list[OrderBookOrder]:
        """Sort orders in descending order by price."""
        return sorted(v, key=lambda x: x.price)


class AbstractOrderBookState(AbstractPairState):
    """This class is largely used for OB dexes that have a batcher."""

    sell_book: SellOrderBook | None = None
    buy_book: BuyOrderBook | None = None
    sell_book_full: SellOrderBook
    buy_book_full: BuyOrderBook

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,  # noqa: ARG002
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
            error_msg = "Asset should only have one token."
            raise ValueError(error_msg)
        if asset.unit() not in [self.unit_a, self.unit_b]:
            error_msg = (
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}"
            )
            raise ValueError(error_msg)

        if asset.unit() == self.unit_a:
            book = self.sell_book_full
            unit_out = self.unit_b
        else:
            book = self.buy_book_full
            unit_out = self.unit_a

        in_quantity = asset.quantity()
        if apply_fee:
            fee = self.fee if self.fee is not None else 0
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
        precise: bool = True,  # noqa: ARG002
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
        if len(asset) != ONE_VALUE:
            error_msg = "Asset should only have one token."
            raise ValueError(error_msg)
        if asset.unit() not in [self.unit_a, self.unit_b]:
            error_msg = (
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}"
            )
            raise ValueError(error_msg)

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
        buy_price = Decimal(0)
        sell_price = Decimal(0)

        if self.buy_book is not None and self.buy_book[0] is not None:
            buy_price = self.buy_book[0].price

        if self.sell_book is not None and self.sell_book[0] is not None:
            sell_price = self.sell_book[0].price
        return (
            Decimal((buy_price + 1 / sell_price) / 2),
            Decimal((sell_price + 1 / buy_price) / 2),
        )

    @property
    def tvl(self) -> Decimal:
        """Return the total value locked for the pool.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        if self.unit_a != "lovelace":
            error_msg = "tvl for non-ADA pools is not implemented."
            raise NotImplementedError(error_msg)

        if self.buy_book is None or self.sell_book is None:
            return Decimal(0)

        tvl = sum(b.quantity / b.price for b in self.buy_book if b is not None) + sum(
            s.quantity * s.price for s in self.sell_book if s is not None
        )

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
            assets: Optional. The assets associated with the order book. Defaults to None.
            orders: Optional. A list of orders to initialize the order book. Defaults to None.

        Returns:
            AbstractOrderBookState: An instance of an abstract order book state.

        Raises:
            NotImplementedError: If the method is not implemented in the subclass.
        """
        raise NotImplementedError
