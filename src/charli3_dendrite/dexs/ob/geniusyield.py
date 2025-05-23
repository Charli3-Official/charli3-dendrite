"""Geniues Yield Order Book Module."""

import time
from dataclasses import dataclass
from dataclasses import field
from functools import lru_cache
from math import ceil
from math import floor
from typing import Dict
from typing import List
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

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import CancelRedeemer
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderState
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.utility import asset_to_value


@dataclass
class GeniusTxRef(PlutusData):
    CONSTR_ID = 0
    tx_hash: bytes


@dataclass
class GeniusUTxORef(PlutusData):
    CONSTR_ID = 0
    tx_ref: GeniusTxRef
    index: int

    def __hash__(self) -> bytes:
        return hash(self.hash().payload)

    def __eq__(self, other):
        if isinstance(other, GeniusUTxORef):
            return self.hash() == other.hash()
        else:
            return False


@dataclass
class GeniusSubmitRedeemer(PlutusData):
    CONSTR_ID = 1
    spend_amount: int


@dataclass
class GeniusCompleteRedeemer(PlutusData):
    CONSTR_ID = 2


@dataclass
class GeniusContainedFee(PlutusData):
    CONSTR_ID = 0
    lovelaces: int
    offered_tokens: int
    asked_tokens: int


@dataclass
class GeniusTimestamp(PlutusData):
    CONSTR_ID = 0
    timestamp: int


@dataclass
class GeniusRational(PlutusData):
    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class GeniusYieldOrder(OrderDatum):
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
        return self.offered_asset.assets + self.asked_asset.assets

    def address_source(self) -> str | None:
        return None

    def requested_amount(self) -> Assets:
        asset = self.offered_asset.assets
        return asset

    def order_type(self) -> OrderType | None:
        order_type = None
        if self.offered_original_amount == self.offered_amount:
            order_type = OrderType.deposit
        else:
            order_type = OrderType.swap

        return order_type


@dataclass
class GeniusYieldFeeDatum(PlutusData):
    CONSTR_ID = 0
    fees: Dict[GeniusUTxORef, Dict[bytes, Dict[bytes, int]]]
    reserved_value: Dict[bytes, Dict[bytes, int]]
    spent_utxo: Union[GeniusUTxORef, PlutusNone] = field(default_factory=PlutusNone)


@dataclass
class GeniusYieldSettings(PlutusData):
    CONSTR_ID = 0
    signatories: List[bytes]
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
    fee: int = 30

    _batcher: Assets = Assets(lovelace=1000000)
    _datum_parsed: PlutusData | None = None
    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """The dex nft policy.

        This should be the policy or policy+name of the dex nft.

        If None, then the default dex nft check is skipped.

        Returns:
            Optional[str]: policy or policy+name of dex nft
        """
        return [
            "22f6999d4effc0ade05f6e1a70b702c65d6b3cdf0e301e4a8267f585",  # v1
            "642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503",  # v1.1
        ]

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "GeniusYield"

    @property
    def volume_fee(self) -> float:
        return self.fee

    @property
    def reference_utxo(self) -> UTxO | None:
        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.dex_nft.unit()],
            addresses=self.pool_selector().addresses,
        )

        script = get_backend().get_script_from_address(
            Address.decode(order_info[0].address),
        )

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
        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.dex_nft.unit()],
            addresses=self.pool_selector().addresses,
        )

        if (
            Address.decode(order_info[0].address).payment_part.payload.hex()
            == "a8d7ff5cf4c117270288372f9ac8e1ce5b758ae15a7851c41661a48c"
        ):
            address = Address.decode(
                "addr1wxcqkdhe7qcfkqcnhlvepe7zmevdtsttv8vdfqlxrztaq2gge58rd",
            )
            asset = "fae686ea8f21d567841d703dea4d4221c2af071a6f2b433ff07c0af2682fd5d4b0d834a3aa219880fa193869b946ffb80dba5532abca0910c55ad5cd"
        else:
            address = Address.decode(
                "addr1w9zr09hgj7z6vz3d7wnxw0u4x30arsp5k8avlcm84utptls8uqd0z",
            )
            asset = "fae686ea8f21d567841d703dea4d4221c2af071a6f2b433ff07c0af24aff78908ef2dce98bfe435fb3fd2529747b1c4564dff5adebedf4e46d0fc63d"

        script = get_backend().get_datum_from_address(address, asset=asset)

        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=script.tx_index,
            ),
            output=TransactionOutput(
                address=script.address,
                amount=asset_to_value(script.assets),
                datum=RawPlutusData.from_cbor(script.datum_cbor),
            ),
        )

    @property
    def mint_reference_utxo(self) -> UTxO | None:
        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.dex_nft.unit()],
            **self.pool_selector().model_dump(exclude_defaults=True),
        )
        script = get_backend().get_script_from_address(
            Address(
                payment_part=ScriptHash(
                    payload=bytes.fromhex(self.dex_nft.unit()[:56]),
                ),
            ),
        )

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
        script = get_backend().get_datum_from_address(
            address=Address.decode(
                "addr1wxcqkdhe7qcfkqcnhlvepe7zmevdtsttv8vdfqlxrztaq2gge58rd",
            ),
            asset="fae686ea8f21d567841d703dea4d4221c2af071a6f2b433ff07c0af2682fd5d4b0d834a3aa219880fa193869b946ffb80dba5532abca0910c55ad5cd",
        )

        datum = RawPlutusData.from_cbor(script.datum_cbor)
        return GeniusYieldSettings.from_cbor(script.datum_cbor)

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.dex_nft.unit()],
            addresses=self.pool_selector().addresses,
        )

        payment = self._get_closest_input(in_assets)
        fee = self._get_fee(payment)

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
                GeniusSubmitRedeemer(spend_amount=out_assets.quantity()),
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
        order_datum.offered_amount -= out_assets.quantity()
        order_datum.partial_fills += 1
        order_datum.contained_fee.lovelaces += self.order_datum.maker_lovelace_fee
        order_datum.contained_fee.asked_tokens += fee.quantity()
        order_datum.contained_payment += payment.quantity()
        assets.root[in_assets.unit()] += (payment + fee).quantity()
        assets.root[out_assets.unit()] -= out_assets.quantity()
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
                    self.in_unit: ceil(order_datum.contained_payment),
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
                },
            )
            fee_assets += Assets(
                **{
                    self.out_unit: self.order_datum.contained_fee.offered_tokens,
                },
            )
            fee_assets += Assets(
                **{self.in_unit: self.order_datum.contained_fee.asked_tokens},
            )
            fee_address = settings.fee_address.to_address()
            asset_value = asset_to_value(fee_assets).to_primitive()
            asset_dict = {b"": {b"": asset_value[0]}}
            # asset_dict.update(asset_value[1])
            for policy, v in asset_value[1].items():
                for name, quantity in v.items():
                    if quantity > 0:
                        asset_dict.update(asset_value[1])
            fee_datum = GeniusYieldFeeDatum(
                fees={pay_datum: asset_dict},
                reserved_value={},
            )
            fee_assets.root["lovelace"] += 1000000
            fee_assets += fee
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
    def post_init(cls, values: dict[str, ...]):
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

    def _get_fee(self, in_assets: Assets) -> Assets:
        """Get the fee for the transaction."""
        # Calculate the fee, round up for v1
        if self.dex_nft.unit().startswith(
            "22f6999d4effc0ade05f6e1a70b702c65d6b3cdf0e301e4a8267f585",
        ):
            fee = Assets(
                root={self.in_unit: ceil(in_assets.quantity() * self.fee / 10000)},
            )
        else:
            fee = Assets(root={self.in_unit: in_assets.quantity() * self.fee // 10000})

        return fee

    def _get_closest_input(self, asset: Assets) -> tuple[Assets, Assets]:
        """Get the closest input amount to the asset."""
        num, denom = self.price

        # Calculate the largest amount without going over the input amount
        if self.dex_nft.unit().startswith(
            "22f6999d4effc0ade05f6e1a70b702c65d6b3cdf0e301e4a8267f585",
        ):
            amount_in = Assets(
                root={self.in_unit: asset.quantity() * 10000 // (self.fee + 10000)},
            )
        else:
            amount_in = Assets(
                root={
                    self.in_unit: ceil(asset.quantity() * 10000 / (self.fee + 10000)),
                },
            )

        # Quantize to the nearest unit
        amount_in.root[self.in_unit] = ceil(
            (amount_in.quantity() * denom // num) * num / denom,
        )

        return amount_in

    @lru_cache(64)
    def get_amount_out(self, asset: Assets, precise=False) -> tuple[Assets, float]:
        amount_in = self._get_closest_input(asset)

        # v1.1 seem to ceil the input rather than floor, so account for that
        if self.dex_nft.unit().startswith(
            "642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503",
        ):
            amount_in.root[self.in_unit] -= 1

        # We want the handle the fee ourself, so set to 0 so no fees are calculated
        fee = self.fee
        self.fee = 0
        amount_out, slippage = super().get_amount_out(asset=amount_in, precise=precise)
        self.fee = fee

        # Depending on contract version, we need to ceil or floor the output
        if self.dex_nft.unit().startswith(
            "642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503",
        ):
            amount_out.root[self.out_unit] = ceil(amount_out.quantity())
        else:
            amount_out.root[self.out_unit] = floor(amount_out.quantity())

        return amount_out, slippage

    @lru_cache(64)
    def get_amount_in(self, asset: Assets, precise=True) -> tuple[Assets, float]:
        # We want the handle the fee ourself, so set to 0 so no fees are calculated
        fee = self.fee
        self.fee = 0
        amount_in, _ = super().get_amount_in(asset=asset, precise=precise)
        self.fee = fee

        fee = self._get_fee(amount_in)

        amount_in = self._get_closest_input(amount_in + fee)

        return amount_in + fee, 0

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return [
            "addr1wx5d0l6u7nq3wfcz3qmjlxkgu889kav2u9d8s5wyzes6frqktgru2",
            "addr1w8kllanr6dlut7t480zzytsd52l7pz4y3kcgxlfvx2ddavcshakwd",
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=[
                "addr1wx5d0l6u7nq3wfcz3qmjlxkgu889kav2u9d8s5wyzes6frqktgru2",
                "addr1w8kllanr6dlut7t480zzytsd52l7pz4y3kcgxlfvx2ddavcshakwd",
            ],
        )

    @property
    def swap_forward(self) -> bool:
        return True

    @property
    def stake_address(self) -> Address | None:
        return None

    @classmethod
    def order_datum_class(self) -> type[PlutusData]:
        return GeniusYieldOrder

    @classmethod
    def default_script_class(self) -> type[PlutusV1Script] | type[PlutusV2Script]:
        return PlutusV2Script

    @property
    def price(self) -> tuple[int, int]:
        # if self.assets.unit() == Assets.model_validate(self.assets.model_dump()).unit():
        return [
            self.order_datum.price.numerator,
            self.order_datum.price.denominator,
        ]

    @property
    def available(self) -> Assets:
        """Max amount of output asset that can be used to fill the order."""
        return Assets(**{self.out_unit: self.order_datum.offered_amount})

    @property
    def tvl(self) -> int:
        """Return the total value locked in the order

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
        return self.dex_nft.unit()


class GeniusYieldOrderBook(AbstractOrderBookState):
    fee: int = 30
    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def get_book(
        cls,
        assets: Assets,
        orders: list[GeniusYieldOrderState] | None,
    ) -> "GeniusYieldOrderBook":
        if orders is None:
            selector = GeniusYieldOrderState.pool_selector()

            result = get_backend().get_pool_utxos(
                limit=10000,
                historical=False,
                **selector.model_dump(),
            )

            orders = [
                GeniusYieldOrderState.model_validate(r.model_dump()) for r in result
            ]

        # sort orders into buy and sell
        buy_orders = []
        sell_orders = []
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
        return "GeniusYield"

    @classmethod
    def order_selector(self) -> list[str]:
        """Order selection information."""
        return GeniusYieldOrderState.order_selector()

    @classmethod
    def pool_selector(self) -> PoolSelector:
        """Pool selection information."""
        return GeniusYieldOrderState.pool_selector()

    @property
    def swap_forward(self) -> bool:
        return False

    @classmethod
    def default_script_class(cls):
        return GeniusYieldOrderState.default_script_class

    @classmethod
    def order_datum_class(cls):
        return GeniusYieldOrderState.order_datum_class()

    @property
    def pool_id(self) -> str:
        return "GeniusYield"

    @property
    def stake_address(self) -> Address | None:
        return None

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = True,
    ) -> tuple[Assets, float]:
        return super().get_amount_out(asset=asset, precise=precise, apply_fee=apply_fee)

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = True,
    ) -> tuple[Assets, float]:
        return super().get_amount_in(asset=asset, precise=precise, apply_fee=apply_fee)

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
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

            if fee_txo is not None:
                txo.amount.coin -= 1000000

                if not isinstance(datum, GeniusYieldFeeDatum):
                    datum.contained_fee.lovelaces -= 1000000

            in_total -= order_in

            if in_total.quantity() <= state.price[0] / state.price[1]:
                break

        if fee_txo is not None:
            if isinstance(datum, GeniusYieldFeeDatum):
                fee_txo.amount += txo.amount
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
