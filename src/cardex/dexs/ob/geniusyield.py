"""Data classes and utilities for GeniusYield.

This contains data classes and utilities for handling various order and pool datums
"""
import time
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from math import ceil
from typing import Any
from typing import Optional
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano.utils import min_lovelace

from cardex.backend.dbsync import get_datum_from_address
from cardex.backend.dbsync import get_pool_in_tx
from cardex.backend.dbsync import get_pool_utxos
from cardex.backend.dbsync import get_script_from_address
from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import CancelRedeemer
from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dataclasses.models import PoolSelectorType
from cardex.dexs.ob.ob_base import AbstractOrderBookState
from cardex.dexs.ob.ob_base import AbstractOrderState
from cardex.dexs.ob.ob_base import BuyOrderBook
from cardex.dexs.ob.ob_base import OrderBookOrder
from cardex.dexs.ob.ob_base import SellOrderBook
from cardex.utility import asset_to_value


@dataclass
class GeniusTxRef(PlutusData):
    """Represent a Genius transaction reference."""

    CONSTR_ID = 0
    tx_hash: bytes


@dataclass
class GeniusUTxORef(PlutusData):
    """Represent a Genius UTXO (Unspent Transaction Output) reference."""

    CONSTR_ID = 0
    tx_ref: GeniusTxRef
    index: int

    def __hash__(self) -> int:
        """The hash of the UTXO reference."""
        return hash(self.hash().payload)

    def __eq__(self, other: object) -> bool:
        """Compare this UTXO reference with another for equality."""
        if isinstance(other, GeniusUTxORef):
            return self.hash() == other.hash()
        return False


@dataclass
class GeniusSubmitRedeemer(PlutusData):
    """Represent a submit redeemer in Genius."""

    CONSTR_ID = 1
    spend_amount: int


@dataclass
class GeniusCompleteRedeemer(PlutusData):
    """Represent a complete redeemer in Genius."""

    CONSTR_ID = 2


@dataclass
class GeniusContainedFee(PlutusData):
    """Represent contained fees in Genius."""

    CONSTR_ID = 0
    lovelaces: int
    offered_tokens: int
    asked_tokens: int


@dataclass
class GeniusTimestamp(PlutusData):
    """Represent a timestamp."""

    CONSTR_ID = 0
    timestamp: int


@dataclass
class GeniusRational(PlutusData):
    """Represent a rational number."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class GeniusYieldOrder(OrderDatum):
    """Represent a yield order in Genius."""

    CONSTR_ID = 0
    owner_key: bytes
    owner_address: PlutusFullAddress
    offered_asset: AssetClass
    offered_original_amount: int
    offered_amount: int
    asked_asset: AssetClass
    price: GeniusRational
    nft: bytes
    start_time: Union[GeniusTimestamp, PlutusNone]
    end_time: Union[GeniusTimestamp, PlutusNone]
    partial_fills: int
    maker_lovelace_fee: int
    taker_lovelace_fee: int
    contained_fee: GeniusContainedFee
    contained_payment: int

    def pool_pair(self) -> Assets | None:
        """Get the pool pair."""
        return self.offered_asset.assets + self.asked_asset.assets

    def address_source(self) -> str | None:
        """Get the address source of the order."""
        return None

    def requested_amount(self) -> Assets:
        """Get the requested amount."""
        return self.offered_asset.assets

    def order_type(self) -> OrderType:
        """Get the type of the order."""
        return OrderType.swap


@dataclass
class GeniusYieldFeeDatum(PlutusData):
    """represent yield fee data in Genius."""

    CONSTR_ID = 0
    fees: dict[GeniusUTxORef, dict[bytes, dict[bytes, int]]]
    reserved_value: dict[bytes, dict[bytes, int]]
    spent_utxo: Union[GeniusUTxORef, PlutusNone] = field(default_factory=PlutusNone())


@dataclass
class GeniusYieldSettings(PlutusData):
    """Represent yield settings in Genius."""

    CONSTR_ID = 0
    signatories: list[bytes]
    req_signatories: int
    nft_symbol: bytes
    fee_address: PlutusFullAddress
    maker_fee_flat: int
    maker_fee_ratio: GeniusRational
    taker_fee: int
    min_deposit: int


class GeniusYieldOrderState(AbstractOrderState):
    """This class is largely used for OB dexes that allow direct script inputs."""

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False
    fee: int = int(30 / 1.003)

    _batcher: Assets = Assets(lovelace=1000000)
    _datum_parsed: PlutusData | None = None
    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def dex_policy(cls) -> list[str]:
        """The dex nft policy.

        This should be the policy or policy+name of the dex nft.

        If None, then the default dex nft check is skipped.

        Returns:
            Optional[str]: policy or policy+name of dex nft
        """
        return [
            "22f6999d4effc0ade05f6e1a70b702c65d6b3cdf0e301e4a8267f585",
            "642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503",
        ]

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "GeniusYield"

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Get the reference UTXO."""
        if cls.dex_nft is None:
            return None

        order_info = get_pool_in_tx(cls.tx_hash, assets=[cls.dex_nft.unit()])

        script = get_script_from_address(Address.decode(order_info[0].address))

        if script.tx_hash is None or script.script is None or script.assets is None:
            return None

        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=script.tx_index,
            ),
            output=TransactionOutput(
                address=script.address,
                amount=asset_to_value(script.assets),
                script=PlutusV2Script(bytes.fromhex(script.script)),
            ),
        )

    @property
    def fee_reference_utxo(self) -> UTxO | None:
        """Get the fee reference UTXO."""
        if self.dex_nft is None:
            return None

        order_info = get_pool_in_tx(self.tx_hash, assets=[self.dex_nft.unit()])

        script = get_script_from_address(Address.decode(order_info[0].address))

        if script.tx_hash is None or script.script is None or script.assets is None:
            return None

        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=0,
            ),
            output=TransactionOutput(
                address=script.address,
                amount=asset_to_value(
                    Assets(
                        **{
                            "lovelace": 2133450,
                            "fae686ea8f21d567841d703dea4d4221c2af071a6f2b433ff07c0af2682fd5d4b0d834a3aa219880fa193869b946ffb80dba5532abca0910c55ad5cd": 1,
                        },
                    ),
                ),
                datum=RawPlutusData.from_cbor(
                    "d8799f9f581cf43138a5c2f37cc8c074c90a5b347d7b2b3ebf729a44b9bbdc883787581c7a3c29ca42cc2d4856682a4564c776843e8b9135cf73c3ed9e986aba581c4fd090d48fceef9df09819f58c1d8d7cbf1b3556ca8414d3865a201c581cad27a6879d211d50225f7506534bbb3c8a47e66bbe78ef800dc7b3bcff03581c642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503d8799fd8799f581caf21fa93ded7a12960b09bd1bc95d007f90513be8977ca40c97582d7ffd87a80ff1a000f4240d8799f031903e8ff1a000f42401a00200b20ff",
                ),
            ),
        )

    @property
    def mint_reference_utxo(self) -> UTxO | None:
        """Get the mint reference UTXO."""
        if self.dex_nft is None:
            return None
        order_info = get_pool_in_tx(  # noqa: F841
            self.tx_hash,
            assets=[self.dex_nft.unit()],
        )
        script = get_script_from_address(
            Address(
                payment_part=ScriptHash(
                    payload=bytes.fromhex(self.dex_nft.unit()[:56]),
                ),
            ),
        )

        if script.tx_hash is None or script.script is None or script.assets is None:
            return None

        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=1,
            ),
            output=TransactionOutput(
                address=script.address,
                amount=asset_to_value(script.assets),
                script=PlutusV2Script(bytes.fromhex(script.script)),
            ),
        )

    @property
    def settings_datum(self) -> GeniusYieldSettings:
        """Get the settings datum."""
        script = get_datum_from_address(
            address=Address.decode(
                "addr1wxcqkdhe7qcfkqcnhlvepe7zmevdtsttv8vdfqlxrztaq2gge58rd",
            ),
            asset="fae686ea8f21d567841d703dea4d4221c2af071a6f2b433ff07c0af2682fd5d4b0d834a3aa219880fa193869b946ffb80dba5532abca0910c55ad5cd",
        )

        from pycardano import RawPlutusData

        datum = RawPlutusData.from_cbor(script.datum_cbor)  # noqa: F841
        return GeniusYieldSettings.from_cbor(script.datum_cbor)

    def swap_utxo(  # noqa: PLR0913, PLR0915
        self,
        address_source: Address,  # noqa: ARG002
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: Optional[TransactionBuilder] = None,
        extra_assets: Assets | None = None,  # noqa: ARG002
        address_target: Address | None = None,  # noqa: ARG002
        datum_target: PlutusData | None = None,  # noqa: ARG002
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Creates the swap UTXO."""
        if self.dex_nft is None:
            error_msg = "Dex nft is none."
            raise ValueError(error_msg)

        if tx_builder is None:
            error_msg = "TransactionBuilder is required for this operation"
            raise ValueError(error_msg)

        order_info = get_pool_in_tx(self.tx_hash, assets=[self.dex_nft.unit()])

        # Ensure the output matches required outputs
        out_check, _ = self.get_amount_out(asset=in_assets)
        if out_check.quantity() != out_assets.quantity():
            error_msg = "Output quantity does not match required outputs."
            raise ValueError(error_msg)

        # Ensure user is not overpaying
        in_check, _ = self.get_amount_in(asset=out_assets)
        if (
            in_assets.quantity() - in_check.quantity() != 0
        ):  # <= self.price[0] / self.price[1]
            error_msg = "User is overpaying."
            raise ValueError(error_msg)

        in_assets = in_check

        assets = self.assets + Assets(**{self.dex_nft.unit(): 1})
        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(assets),
                datum_hash=self.order_datum.hash(),
            ),
        )

        if out_assets.quantity() < self.available.quantity():
            redeemer = Redeemer(
                GeniusSubmitRedeemer(spend_amount=out_assets.quantity() + 1),
            )
        else:
            redeemer = Redeemer(GeniusCompleteRedeemer())
        tx_builder.add_script_input(
            utxo=input_utxo,
            script=self.reference_utxo,
            redeemer=redeemer,
        )

        tx_builder.reference_inputs.add(self.fee_reference_utxo)

        order_datum = self.order_datum_class().from_cbor(self.order_datum.to_cbor())
        order_datum.offered_amount -= out_assets.quantity() + 1
        order_datum.partial_fills += 1
        order_datum.contained_fee.lovelaces += 1000000
        if self.volume_fee is None:
            error_msg = "Volume fee is not defined."
            raise ValueError(error_msg)
        order_datum.contained_fee.asked_tokens += (
            int(in_assets.quantity() * self.volume_fee) // 10000
        )
        order_datum.contained_payment += (
            int(in_assets.quantity() * (10000 - self.volume_fee)) // 10000
        ) + 1
        assets.root[in_assets.unit()] += in_assets.quantity()
        assets.root[out_assets.unit()] -= out_assets.quantity() + 1
        assets += self._batcher

        if out_assets.quantity() < self.available.quantity():
            txo = TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(assets),
                datum_hash=order_datum.hash(),
            )
        else:
            settings = self.settings_datum

            # Burn the beacon token
            tx_builder.add_minting_script(
                script=self.mint_reference_utxo,
                redeemer=Redeemer(CancelRedeemer()),
            )
            if tx_builder.mint is None:
                tx_builder.mint = asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset
            else:
                tx_builder.mint += asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset

            # Pay the order owner
            payment_assets = Assets(**{"lovelace": settings.min_deposit})

            payment_assets += Assets(
                **{
                    self.in_unit: ceil(
                        (
                            self.price[0] * self.order_datum.offered_original_amount
                            + (self.price[1] - 1)
                        )
                        / self.price[1],
                    ),
                },
            )
            pay_datum = GeniusUTxORef(
                tx_ref=GeniusTxRef(tx_hash=bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            )
            txo = TransactionOutput(
                address=order_datum.owner_address.to_address(),
                amount=asset_to_value(payment_assets),
                datum_hash=pay_datum.hash(),
            )
            tx_builder.datums.update({pay_datum.hash(): pay_datum})
            tx_builder.add_output(txo)

            # Pay the protocol fees
            fee_assets = Assets(
                **{
                    "lovelace": self.order_datum.contained_fee.lovelaces,
                    self.out_unit: self.order_datum.contained_fee.offered_tokens,
                },
            )
            fee_assets += Assets(
                **{self.in_unit: self.order_datum.contained_fee.asked_tokens},
            )
            fee_address = settings.fee_address.to_address()
            asset_value = asset_to_value(fee_assets).to_primitive()
            asset_dict = {b"": {b"": asset_value[0]}}
            asset_dict.update(asset_value[1])
            fee_datum = GeniusYieldFeeDatum(
                fees={pay_datum: asset_dict},
                reserved_value={},
            )
            fee_assets.root["lovelace"] += 1000000
            fee_assets += Assets(
                **{self.in_unit: (in_assets.quantity() * self.fee) // 10000},
            )
            fee_txo = TransactionOutput(
                address=fee_address,
                amount=asset_to_value(assets=fee_assets),
                datum_hash=fee_datum.hash(),
            )
            min_ada = min_lovelace(tx_builder.context, output=fee_txo)
            if fee_txo.amount.coin < min_ada:
                fee_txo.amount.coin = min_ada

            txo = fee_txo
            order_datum = fee_datum

        tx_builder.datums.update({self.order_datum.hash(): self.order_datum})

        return txo, order_datum

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Performs post-initialization checks and updates.

        Args:
            values (dict[str, Any]): The pool initialization parameters.

        Returns:
            dict[str, Any]: Updated pool initialization parameters.
        """
        super().post_init(values)
        datum = cls.order_datum_class().from_cbor(values["datum_cbor"])

        ask_unit = datum.asked_asset.assets.unit()
        offer_unit = datum.offered_asset.assets.unit()

        if values["assets"].unit() != ask_unit:
            quantity = values["assets"].root.pop(offer_unit)
            values["assets"].root[offer_unit] = quantity

        values["inactive"] = False
        if (
            datum.start_time.CONSTR_ID == 0
            and datum.start_time.timestamp / 1000 > time.time()
        ):
            values["inactive"] = True
        if (
            datum.end_time.CONSTR_ID == 0
            and datum.end_time.timestamp / 1000 < time.time()
        ):
            values["inactive"] = True

        return values

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Calculates the amount out and slippage for given input asset."""
        amount_out, slippage = super().get_amount_out(asset=asset, precise=precise)

        if self.price[0] / self.price[1] > 1:
            new_asset = Assets.model_validate(asset.model_dump())
            new_asset.root[self.in_unit] += 1
            new_amount_out, _ = super().get_amount_out(asset=new_asset, precise=precise)

            if new_amount_out.quantity() == amount_out.quantity():
                while amount_out.quantity() == new_amount_out.quantity():
                    new_asset.root[self.in_unit] -= 1
                    new_amount_out, _ = super().get_amount_out(
                        asset=new_asset,
                        precise=precise,
                    )

                amount_out = new_amount_out

        return amount_out, slippage

    def get_amount_in(
        self,
        asset: Assets,
        precise=False,  # noqa: ANN001
    ) -> tuple[Assets, float]:
        """Calculates the amount in and slippage for given output asset.

        Args:
            asset (Assets): The output asset.
            precise (bool, optional): Whether to calculate precisely. Defaults to True.

        Returns:
            tuple[Assets, float]: The amount in and slippage.
        """
        fee = self.fee
        self.fee = int(self.fee * 1.003)
        amount_in, slippage = super().get_amount_in(asset=asset, precise=precise)
        self.fee = fee

        amount_in.root[self.in_unit] = ceil(amount_in.quantity())

        # get_amount_out is correct, this corrects nominal errors
        amount_out, _ = self.get_amount_out(asset=amount_in)
        while (
            amount_out.quantity() < asset.quantity()
            and amount_out.quantity() < self.available.quantity()
        ):
            amount_in.root[self.in_unit] += 1
            amount_out, _ = self.get_amount_out(asset=amount_in)

        return amount_in, slippage

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return [
            "addr1wx5d0l6u7nq3wfcz3qmjlxkgu889kav2u9d8s5wyzes6frqktgru2",
            "addr1w8kllanr6dlut7t480zzytsd52l7pz4y3kcgxlfvx2ddavcshakwd",
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        return PoolSelector(
            selector_type=PoolSelectorType.address,
            selector=cls.order_selector(),
        )

    @property
    def swap_forward(self) -> bool:
        """Returns True."""
        return True

    @property
    def stake_address(self) -> Address | None:
        """Represents stake_address. Returns None."""
        return None

    @classmethod
    def order_datum_class(cls) -> type[GeniusYieldOrder]:
        """Returns the class type of order."""
        return GeniusYieldOrder

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the default script class for the pool."""
        return PlutusV2Script

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Get the price of the order as a tuple of numerator and denominator."""
        # if self.assets.unit() == Assets.model_validate(self.assets.model_dump()).unit():
        return (
            self.order_datum.price.numerator,
            self.order_datum.price.denominator,
        )

    @property
    def available(self) -> Assets:
        """Max amount of output asset that can be used to fill the order."""
        return Assets(**{self.out_unit: self.order_datum.offered_amount})

    @property
    def tvl(self) -> Decimal:
        """Return the total value locked in the order.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        return self.available

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        if self.dex_nft is None:
            error_msg = "Dex NFT is none."
            raise ValueError(error_msg)
        return self.dex_nft.unit()


class GeniusYieldOrderBook(AbstractOrderBookState):
    """Represents Order book."""

    fee: int = int(30 / 1.003)
    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def get_book(
        cls,
        assets: Assets | None = None,
        orders: list[GeniusYieldOrderState] | None = None,
    ) -> "GeniusYieldOrderBook":
        """Retrieve and sort orders into buy and sell categories."""
        if orders is None:
            selector = GeniusYieldOrderState.pool_selector()
            selector_dict = selector.to_dict()

            result = get_pool_utxos(
                addresses=selector_dict.get("addresses"),
                assets=selector_dict.get("assets"),
                limit=1,
                historical=False,
            )

            orders = [
                GeniusYieldOrderState.model_validate(r.model_dump()) for r in result
            ]

        # sort orders into buy and sell
        buy_orders = []
        sell_orders = []

        if assets is None:
            error_msg = "Assets cannot be None."
            raise ValueError(error_msg)

        for order in orders:
            if order.inactive:
                continue
            price = order.price[0] / order.price[1]
            o = OrderBookOrder(
                price=price,
                quantity=int(order.available.quantity()),
                state=order,
            )
            if order.in_unit == assets.unit() and order.out_unit == assets.unit(1):
                sell_orders.append(o)
            elif order.in_unit == assets.unit(1) and order.out_unit == assets.unit(0):
                buy_orders.append(o)

        ob = GeniusYieldOrderBook(
            assets=assets,
            plutus_v2=False,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook(sell_orders),
            buy_book_full=BuyOrderBook(buy_orders),
        )

        # GeniusYield recommends using a max of 3 orders in one tx because of mem limits
        ob.sell_book_full = ob.sell_book_full[:3]
        ob.buy_book_full = ob.buy_book_full[:3]

        return ob

    @classmethod
    def dex(cls) -> str:
        """Returns dex name."""
        return "GeniusYield"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return GeniusYieldOrderState.order_selector()

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        return GeniusYieldOrderState.pool_selector()

    @property
    def swap_forward(self) -> bool:
        """Represents swap forward. Returns False."""
        return False

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the default script class."""
        return GeniusYieldOrderState.default_script_class()

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns the class type of order datum."""
        return GeniusYieldOrderState.order_datum_class()

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return "GeniusYield"

    @property
    def stake_address(self) -> Address | None:
        """Get the stake address."""
        return None

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = True,
    ) -> tuple[Assets, float]:
        """Calculates the amount out and slippage for given input asset."""
        return super().get_amount_out(asset=asset, precise=precise, apply_fee=apply_fee)

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = True,
    ) -> tuple[Assets, float]:
        """Calculates the amount in and slippage for given input asset."""
        return super().get_amount_in(asset=asset, precise=precise, apply_fee=apply_fee)

    def swap_utxo(  # noqa: PLR0913, PLR0912
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,  # noqa: ARG002
        tx_builder: Optional[TransactionBuilder] = None,
        extra_assets: Assets | None = None,  # noqa: ARG002
        address_target: Address | None = None,  # noqa: ARG002
        datum_target: PlutusData | None = None,  # noqa: ARG002
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Swap utxo that generates a transaction output representing the swap."""
        if tx_builder is None:
            error_msg = "TransactionBuilder is required for this operation"
            raise ValueError(error_msg)

        if in_assets.unit() == self.assets.unit():
            book = self.sell_book_full
        else:
            book = self.buy_book_full

        in_total = Assets.model_validate(in_assets.model_dump())
        fee_txo: TransactionOutput | None = None
        fee_datum: GeniusYieldFeeDatum | None = None
        txo: TransactionOutput | None = None
        datum = None

        for order in book:
            if txo is not None:
                if fee_txo is None:
                    fee_txo = txo
                    fee_datum = datum
                else:
                    fee_txo.amount += txo.amount
                    if fee_datum is not None and datum is not None:
                        fee_datum.fees.update(datum.fees)
                    tx_builder._minting_script_to_redeemers.pop()

            state = order.state

            order_out, _ = state.get_amount_out(in_total)
            order_in, _ = state.get_amount_in(order_out)

            txo, datum = state.swap_utxo(
                address_source=address_source,
                in_assets=order_in,
                out_assets=order_out,
                tx_builder=tx_builder,
            )

            txo.amount.coin -= 1000000

            if not isinstance(datum, GeniusYieldFeeDatum):
                datum.contained_fee.lovelaces -= 1000000

            in_total -= order_in

            if in_total.quantity() <= state.price[0] / state.price[1]:
                break

        if isinstance(datum, GeniusYieldFeeDatum):
            if fee_txo is not None and txo is not None:
                fee_txo.amount += txo.amount
            if fee_datum is not None:
                fee_datum.fees.update(datum.fees)
            tx_builder._minting_script_to_redeemers.pop()
            txo = fee_txo
            datum = fee_datum
        else:
            tx_builder.add_output(
                tx_out=fee_txo,
                datum=fee_datum,
                add_datum_to_witness=True,
            )

        return txo, datum
