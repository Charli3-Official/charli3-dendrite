from abc import abstractmethod
from decimal import Decimal

from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import BaseList
from cardex.dataclasses.models import CardexBaseModel
from cardex.dexs.core.base import AbstractPairState
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NotAPoolError
from cardex.utility import Assets
from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import UTxO
from pydantic import model_validator


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
        return self.assets.unit()

    @property
    def out_unit(self) -> str:
        return self.assets.unit(1)

    @property
    @abstractmethod
    def price(self) -> tuple[int, int]:
        raise NotImplementedError

    @property
    @abstractmethod
    def available(self) -> Assets:
        """Max amount of output asset that can be used to fill the order."""
        raise NotImplementedError

    def get_amount_out(self, asset: Assets, precise=True) -> tuple[Assets, float]:
        assert asset.unit() == self.in_unit and len(asset) == 1

        num, denom = self.price
        out_assets = Assets(**{self.out_unit: 0})
        in_quantity = asset.quantity() * (10000 - self.fee) // 10000
        out_assets.root[self.out_unit] = min(
            (in_quantity * denom) // num,
            self.available.quantity(),
        )

        if precise:
            out_assets.root[self.out_unit] = int(out_assets.quantity())

        return out_assets, 0

    def get_amount_in(self, asset: Assets, precise=True) -> tuple[Assets, float]:
        assert asset.unit() == self.out_unit and len(asset) == 1

        denom, num = self.price
        in_assets = Assets(**{self.in_unit: 0})
        out_quantity = asset.quantity()
        in_assets.root[self.in_unit] = (
            min(out_quantity, self.available.quantity()) * denom
        ) / num
        fees = in_assets[self.in_unit] * self.fee / 10000
        in_assets.root[self.in_unit] += fees

        if precise:
            in_assets.root[self.in_unit] = int(in_assets.quantity())

        return in_assets, 0

    @classmethod
    def skip_init(cls, values: dict[str, ...]) -> bool:
        """An initial check to determine if parsing should be carried out.

        Args:
            values: The pool initialization parameters.

        Returns:
            bool: If this returns True, initialization checks will get skipped.
        """
        return False

    @classmethod
    def extract_dex_nft(cls, values: dict[str, ...]) -> Assets | None:
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
        if cls.dex_policy is None:
            dex_nft = None

        # If the dex nft is in the values, it's been parsed already
        elif "dex_nft" in values:
            if not any(
                any(p.startswith(d) for d in cls.dex_policy) for p in values["dex_nft"]
            ):
                raise NotAPoolError("Invalid DEX NFT")
            dex_nft = values["dex_nft"]

        # Check for the dex nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in cls.dex_policy)
            ]
            if len(nfts) < 1:
                raise NotAPoolError(
                    f"{cls.__name__}: Pool must have one DEX NFT token.",
                )
            dex_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["dex_nft"] = dex_nft

        return dex_nft

    @property
    def order_datum(self) -> PlutusData:
        if self._datum_parsed is None:
            self._datum_parsed = self.order_datum_class.from_cbor(self.datum_cbor)
        return self._datum_parsed

    @classmethod
    def post_init(cls, values: dict[str, ...]):
        """Post initialization checks.

        Args:
            values: The pool initialization parameters
        """
        assets = values["assets"]
        non_ada_assets = [a for a in assets if a != "lovelace"]

        if len(assets) == 2:
            # ADA pair
            assert (
                len(non_ada_assets) == 1
            ), f"Pool must only have 1 non-ADA asset: {values}"

        elif len(assets) == 3:
            # Non-ADA pair
            assert len(non_ada_assets) == 2, "Pool must only have 2 non-ADA assets."

            # Send the ADA token to the end
            values["assets"].root["lovelace"] = values["assets"].root.pop("lovelace")

        else:
            if len(assets) == 1 and "lovelace" in assets:
                raise NoAssetsError(
                    f"Invalid pool, only contains lovelace: assets={assets}",
                )
            else:
                raise InvalidPoolError(
                    f"Pool must have 2 or 3 assets except factor, NFT, and LP tokens: assets={assets}",
                )
        return values

    @model_validator(mode="before")
    def translate_address(cls, values):
        """The main validation function called when initialized.

        Args:
            values: The pool initialization values.

        Returns:
            The parsed/modified pool initialization values.
        """
        if "assets" in values:
            if values["assets"] is None:
                raise NoAssetsError("No assets in the pool.")
            elif not isinstance(values["assets"], Assets):
                values["assets"] = Assets(**values["assets"])

        if cls.skip_init(values):
            return values

        # Parse the order datum
        try:
            datum = cls.order_datum_class.from_cbor(values["datum_cbor"])
        except (DeserializeException, TypeError) as e:
            raise NotAPoolError(
                "Order datum could not be deserialized: \n "
                + f"    error={e}\n"
                + f"    tx_hash={values['tx_hash']}\n"
                + f"    datum={values['datum_cbor']}\n",
            )

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
                    )

        dex_nft = cls.extract_dex_nft(values)

        # Add the pool tokens back in
        values["assets"].root.update(pair.root)

        cls.post_init(values)

        return values


class OrderBookOrder(CardexBaseModel):
    price: float
    quantity: int
    state: AbstractOrderState | None = None


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

        in_quantity = asset.quantity()
        if apply_fee:
            in_quantity = in_quantity * (10000 - self.fee) // 10000

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

        if apply_fee:
            fees = in_assets[unit_in] * self.fee / 10000
            in_assets.root[unit_in] += fees

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

    @classmethod
    @abstractmethod
    def get_book(
        cls,
        assets: Assets | None = None,
        orders: list[AbstractOrderState] | None = None,
    ) -> "AbstractOrderBookState":
        raise NotImplementedError
