"""SaturnSwap Order Book Module.

This module handles the limit order path for SaturnSwap.
"""

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano.utils import min_lovelace

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.core.errors import NotAPoolError
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderState
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.utility import asset_to_value

SATURNSWAP_TAKER_FEE_BPS = 400


@dataclass
class SaturnSwapSomeInt(PlutusData):
    """Some(Int) wrapper for Option<Int>."""

    CONSTR_ID = 0
    value: int


@dataclass
class SaturnSwapOutputReference(PlutusData):
    """Output reference used for double-satisfaction protection."""

    CONSTR_ID = 0
    tx_id: "SaturnSwapTxId"
    index: int


@dataclass
class SaturnSwapTxId(PlutusData):
    """TxId wrapper used in OutputReference."""

    CONSTR_ID = 0
    value: bytes


@dataclass
class SaturnSwapPaymentDatum(PlutusData):
    """PaymentDatum { output_reference }."""

    CONSTR_ID = 0
    output_reference: SaturnSwapOutputReference


@dataclass
class SaturnSwapSwapDatum(OrderDatum):
    """SwapDatum.

    Fields:
        owner
        policy_id_sell
        asset_name_sell
        amount_sell
        policy_id_buy
        asset_name_buy
        amount_buy
        valid_before_time (Option<Int>)
        output_reference
    """

    CONSTR_ID = 0
    owner: PlutusFullAddress
    policy_id_sell: bytes
    asset_name_sell: bytes
    amount_sell: int
    policy_id_buy: bytes
    asset_name_buy: bytes
    amount_buy: int
    valid_before_time: Union[PlutusNone, SaturnSwapSomeInt]
    output_reference: SaturnSwapOutputReference

    def pool_pair(self) -> Assets | None:
        """Return the asset pair for this swap datum."""
        sell_unit = (
            "lovelace"
            if self.policy_id_sell == b""
            else self.policy_id_sell.hex() + self.asset_name_sell.hex()
        )
        buy_unit = (
            "lovelace"
            if self.policy_id_buy == b""
            else self.policy_id_buy.hex() + self.asset_name_buy.hex()
        )
        return Assets(**{sell_unit: 0}) + Assets(**{buy_unit: 0})

    def address_source(self) -> str | None:
        """Return the maker address as a bech32 string."""
        return self.owner.to_address().encode()

    def requested_amount(self) -> Assets:
        """Return the requested buy asset amount."""
        # Maker is requesting buy asset in the amount of amount_buy
        buy_unit = (
            "lovelace"
            if self.policy_id_buy == b""
            else self.policy_id_buy.hex() + self.asset_name_buy.hex()
        )
        return Assets(**{buy_unit: self.amount_buy})

    def order_type(self) -> OrderType | None:
        """Return the order type classification."""
        return OrderType.swap


@dataclass
class SaturnSwapSwapAction(PlutusData):
    """SwapAction(user_sell_amount, input_index, output_index)."""

    CONSTR_ID = 0
    user_sell_amount: int
    input_index: int
    output_index: int

    def set_idx(
        self,
        tx_builder: TransactionBuilder,
    ) -> None:
        """Set the input/output indices from the built transaction.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        owner_address = None
        input_utxo = None

        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.input_index = key.index
                input_utxo = tx_builder.inputs[self.input_index]
                break
        else:
            raise RuntimeError("Swap input UTxO not found")

        for i, tx_in in enumerate(tx_builder.inputs):
            if (
                tx_in.input.transaction_id == input_utxo.input.transaction_id
                and tx_in.input.index == input_utxo.input.index
            ):
                self.input_index = i
                break

        swap_datum = tx_builder.datums[input_utxo.output.datum_hash]
        owner_address = swap_datum.owner.to_address()

        input_tx_id = input_utxo.input.transaction_id
        input_index = input_utxo.input.index

        for i, txo in enumerate(tx_builder.outputs):
            if not isinstance(txo.datum, SaturnSwapPaymentDatum):
                continue
            output_ref = txo.datum.output_reference
            if (
                output_ref.tx_id.value == bytes.fromhex(str(input_tx_id))
                and output_ref.index == input_index
                and txo.address == owner_address
            ):
                self.output_index = i
                return


@dataclass
class SaturnSwapCancelAction(PlutusData):
    """CancelAction(input_index)."""

    CONSTR_ID = 1
    input_index: int

    def set_idx(self, tx_builder: TransactionBuilder) -> None:
        """Set the input index from the built transaction.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.input_index = key.index
                return


class SaturnSwapOrderState(AbstractOrderState):
    """SaturnSwap order state for individual orders."""

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False

    _batcher: Assets = Assets(lovelace=0)
    _datum_parsed: PlutusData | None = None

    @classmethod
    def dex(cls) -> str:
        """Return the DEX name."""
        return "SaturnSwap"

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """SaturnSwap uses parameterized scripts, no global dex NFT."""
        return None

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return order script addresses."""
        return [
            (
                "addr1zyd0sj57d9lpu7cy9g9qdurpazqc9l4eaxk6j59nd2gkh4"
                "275jq4yvpskgayj55xegdp30g5rfynax66r8vgn9fldndsqzf5tn"
            ),
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return pool selector for order UTxOs."""
        return PoolSelector(addresses=cls.order_selector())

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Return datum class used for orders."""
        return SaturnSwapSwapDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV2Script]:
        """Return default script type."""
        return PlutusV2Script

    @property
    def price(self) -> tuple[int, int]:
        """Price as (numerator, denominator) for in_unit/out_unit."""
        amount_sell = int(self.order_datum.amount_sell)
        amount_buy = int(self.order_datum.amount_buy)
        if amount_sell == 0 or amount_buy == 0:
            return (0, 0)
        return (amount_buy, amount_sell)

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Estimate output assets for a given input."""
        if len(asset) != 1:
            raise ValueError("Input asset must contain exactly one unit.")
        if asset.unit() != self.in_unit:
            raise ValueError("Input asset unit must match in_unit.")

        num, denom = self.price
        fee_bps = self.volume_fee

        in_qty = int(asset.quantity())
        out_qty = (in_qty * denom + num - 1) // num
        fee = (out_qty * fee_bps) // 10_000
        out_qty = min(out_qty - fee, int(self.order_datum.amount_sell))
        return Assets(**{self.out_unit: int(out_qty)}), 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Estimate input assets for a desired output."""
        if len(asset) != 1:
            raise ValueError("Output asset must contain exactly one unit.")
        if asset.unit() != self.out_unit:
            raise ValueError("Output asset unit must match out_unit.")

        num, denom = self.price
        fee_bps = self.volume_fee

        desired_out = int(asset.quantity())
        desired_out = min(desired_out, int(self.order_datum.amount_sell))
        gross_out = (desired_out * 10_000 + (10_000 - fee_bps) - 1) // (
            10_000 - fee_bps
        )
        in_qty = (gross_out * num + denom - 1) // denom
        return Assets(**{self.in_unit: int(in_qty)}), 0

    @property
    def available(self) -> Assets:
        """Return available output asset amount."""
        return Assets(**{self.out_unit: self.order_datum.amount_sell})

    @property
    def volume_fee(self) -> int:
        """Fee percentage in basis points."""
        return SATURNSWAP_TAKER_FEE_BPS

    @property
    def swap_forward(self) -> bool:
        """Return whether swaps are forward."""
        return True

    @property
    def stake_address(self) -> Address | None:
        """Return staking address if applicable."""
        return None

    @property
    def tvl(self) -> int:
        """Return total value locked for the order."""
        return int(self.available.quantity())

    @property
    def reference_utxo(self) -> UTxO | None:
        """Get the script reference UTxO (if available)."""
        script_info = get_backend().get_script_from_address(
            Address.decode(self.order_selector()[0]),
        )

        if script_info is None or script_info.script is None:
            return None

        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script_info.tx_hash)),
                index=script_info.tx_index,
            ),
            output=TransactionOutput(
                address=script_info.address,
                amount=asset_to_value(script_info.assets),
                script=self.default_script_class()(bytes.fromhex(script_info.script)),
            ),
        )

    @property
    def pool_id(self) -> str:
        """Return identifier for the order pool."""
        datum = self.order_datum
        return datum.policy_id_sell.hex() + datum.asset_name_sell.hex()

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Set inactive flag based on datum fields."""
        datum = cls.order_datum_class().from_cbor(values["datum_cbor"])

        buy_unit = (
            "lovelace"
            if datum.policy_id_buy == b""
            else datum.policy_id_buy.hex() + datum.asset_name_buy.hex()
        )
        sell_unit = (
            "lovelace"
            if datum.policy_id_sell == b""
            else datum.policy_id_sell.hex() + datum.asset_name_sell.hex()
        )
        if values["assets"].unit() != buy_unit:
            quantity = values["assets"].root.pop(sell_unit)
            values["assets"].root[sell_unit] = quantity

        # If expiration exists, mark inactive when expired.
        vbt = datum.valid_before_time
        if isinstance(vbt, SaturnSwapSomeInt):
            import time

            now_ms = int(time.time() * 1000)
            if now_ms >= vbt.value:
                values["inactive"] = True

        # If amounts are zero, treat as inactive.
        if datum.amount_sell == 0 or datum.amount_buy == 0:
            values["inactive"] = True

        return values

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
        """Build swap transaction outputs and redeemer for this order."""
        two_ada = 2_000_000

        if self.reference_utxo is not None:
            tx_builder.reference_inputs.add(self.reference_utxo)

        order_address = (
            get_backend()
            .get_pool_in_tx(
                self.tx_hash,
                addresses=self.pool_selector().addresses,
            )[0]
            .address
        )

        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_address,
                amount=asset_to_value(self.assets),
                datum_hash=self.order_datum.hash(),
            ),
        )
        script_input_lovelace = input_utxo.output.amount.coin

        # Build output reference for payment datum
        output_ref = SaturnSwapOutputReference(
            tx_id=SaturnSwapTxId(value=bytes.fromhex(self.tx_hash)),
            index=self.tx_index,
        )
        payment_datum = SaturnSwapPaymentDatum(output_reference=output_ref)

        user_sell_amount = int(in_assets.quantity())

        # Determine partial fill
        partial = user_sell_amount < self.order_datum.amount_buy

        sell_unit = out_assets.unit()
        buy_unit = in_assets.unit()
        sell_is_ada = sell_unit == "lovelace"

        # Owner payment output (buy asset)
        owner_address = self.order_datum.owner.to_address()
        owner_assets = Assets(**{buy_unit: user_sell_amount})
        new_amount_sell = _ratio_amount(
            self.order_datum.amount_buy,
            user_sell_amount,
            self.order_datum.amount_sell,
        )
        if partial and sell_is_ada and new_amount_sell > two_ada:
            owner_assets.root["lovelace"] = (
                owner_assets.root.get("lovelace", 0) + two_ada
            )
        elif not partial and not sell_is_ada:
            # Full fill when maker is selling a token:
            # owner must receive buy amount plus original script lovelace.
            owner_assets.root["lovelace"] = (
                owner_assets.root.get("lovelace", 0) + script_input_lovelace
            )

        owner_output = TransactionOutput(
            address=owner_address,
            amount=asset_to_value(owner_assets),
            datum=payment_datum,
        )
        owner_output.amount.coin = max(
            owner_output.amount.coin,
            min_lovelace(tx_builder.context, output=owner_output),
        )
        tx_builder.add_output(owner_output)

        # Fee output (sell asset)
        fee_address = Address.decode(
            "addr1q8x4rlqhrq4rhqhnkamw3fdqmzqgum79yragg4gptcjpph"
            "mrc2rpt0exfch4s47fu32amr45vh9wg053hmcx9k7kkcrq6kxftd",
        )
        fee_amount = (new_amount_sell * self.volume_fee) // 10_000
        fee_assets = Assets(**{sell_unit: fee_amount})
        fee_output = TransactionOutput(
            address=fee_address,
            amount=asset_to_value(fee_assets),
            datum=payment_datum,
        )
        fee_output.amount.coin = max(
            fee_output.amount.coin,
            min_lovelace(tx_builder.context, output=fee_output),
        )
        tx_builder.add_output(fee_output)

        # Redeemer uses input and owner-output indices.
        action = SaturnSwapSwapAction(
            user_sell_amount=user_sell_amount,
            input_index=0,
            output_index=0,
        )
        redeemer = Redeemer(action)

        tx_builder.add_script_input(
            utxo=input_utxo,
            script=self.reference_utxo,
            redeemer=redeemer,
        )
        tx_builder.datums.update({self.order_datum.hash(): self.order_datum})

        # Partial fill: create new swap output back to script with updated datum
        if partial:
            new_amount_buy = self.order_datum.amount_buy - user_sell_amount
            new_amount_sell = _ratio_amount(
                self.order_datum.amount_buy,
                new_amount_buy,
                self.order_datum.amount_sell,
            )

            corrected_new_amount_sell = new_amount_sell
            corrected_new_amount_buy = new_amount_buy
            if sell_is_ada and new_amount_sell > two_ada:
                corrected_new_amount_sell = new_amount_sell - two_ada
                corrected_new_amount_buy = _ratio_amount(
                    self.order_datum.amount_sell,
                    corrected_new_amount_sell,
                    self.order_datum.amount_buy,
                )

            new_datum = SaturnSwapSwapDatum(
                owner=self.order_datum.owner,
                policy_id_sell=self.order_datum.policy_id_sell,
                asset_name_sell=self.order_datum.asset_name_sell,
                amount_sell=corrected_new_amount_sell,
                policy_id_buy=self.order_datum.policy_id_buy,
                asset_name_buy=self.order_datum.asset_name_buy,
                amount_buy=corrected_new_amount_buy,
                valid_before_time=self.order_datum.valid_before_time,
                output_reference=output_ref,
            )

            residual_assets = Assets(**{sell_unit: corrected_new_amount_sell})
            residual_output = TransactionOutput(
                address=order_address,
                amount=asset_to_value(residual_assets),
                datum=new_datum,
            )
            if sell_is_ada:
                residual_output.amount.coin = max(
                    residual_output.amount.coin,
                    min_lovelace(tx_builder.context, output=residual_output),
                )
            else:
                residual_output.amount.coin = max(
                    script_input_lovelace,
                    min_lovelace(tx_builder.context, output=residual_output),
                )
            tx_builder.datums.update({new_datum.hash(): new_datum})
            return residual_output, new_datum

        return None, self.order_datum


class SaturnSwapOrderBook(AbstractOrderBookState):
    """SaturnSwap order book aggregating individual orders."""

    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def get_book(
        cls,
        assets: Assets,
        orders: list[SaturnSwapOrderState] | None = None,
    ) -> "SaturnSwapOrderBook":
        """Build an order book from provided orders or backend UTxOs."""
        min_pair_assets = 2
        utxo_limit = 10_000
        if orders is None:
            selector = SaturnSwapOrderState.pool_selector()
            result = get_backend().get_pool_utxos(
                limit=utxo_limit,
                historical=False,
                **selector.model_dump(),
            )
            # Skip invalid/non-order datums that fail deserialization
            orders = []
            for r in result:
                try:
                    orders.append(SaturnSwapOrderState.model_validate(r.model_dump()))
                except NotAPoolError:
                    continue

        buy_orders: list[OrderBookOrder] = []
        sell_orders: list[OrderBookOrder] = []
        for order in orders:
            if len(order.assets) < min_pair_assets:
                datum = order.order_datum
                buy_unit = (
                    "lovelace"
                    if datum.policy_id_buy == b""
                    else datum.policy_id_buy.hex() + datum.asset_name_buy.hex()
                )
                sell_unit = (
                    "lovelace"
                    if datum.policy_id_sell == b""
                    else datum.policy_id_sell.hex() + datum.asset_name_sell.hex()
                )
                order.assets = Assets(**{buy_unit: 0}) + Assets(**{sell_unit: 0})
            if order.inactive:
                continue
            price_a, price_b = order.price
            if price_a == 0 or price_b == 0:
                continue
            if order.in_unit == assets.unit() and order.out_unit == assets.unit(1):
                price = float(price_a)
                side = sell_orders
            elif order.in_unit == assets.unit(1) and order.out_unit == assets.unit(0):
                price = float(price_b)
                side = buy_orders
            else:
                continue
            o = OrderBookOrder(
                price=price,
                quantity=int(order.available.quantity()),
                state=order,
            )
            side.append(o)

        return SaturnSwapOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook(sell_orders),
            buy_book_full=BuyOrderBook(buy_orders),
        )

    @classmethod
    def dex(cls) -> str:
        """Return the DEX name."""
        return "SaturnSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return order script addresses."""
        return SaturnSwapOrderState.order_selector()

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return pool selector for order UTxOs."""
        return SaturnSwapOrderState.pool_selector()

    @classmethod
    def default_script_class(cls) -> type[PlutusV2Script]:
        """Return default script type."""
        return SaturnSwapOrderState.default_script_class()

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Return datum class used for orders."""
        return SaturnSwapOrderState.order_datum_class()

    @property
    def swap_forward(self) -> bool:
        """Return whether swaps are forward."""
        return False

    @property
    def stake_address(self) -> Address | None:
        """Return staking address if applicable."""
        return None

    @property
    def pool_id(self) -> str:
        """Return identifier for the order book."""
        return "SaturnSwap"

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Return mid price of assets based on the full order books."""
        if not self.buy_book_full or not self.sell_book_full:
            return Decimal(0), Decimal(0)
        buy = Decimal(self.buy_book_full[0].price)
        sell = Decimal(self.sell_book_full[0].price)
        return (
            Decimal((buy + (Decimal(1) / sell)) / 2),
            Decimal((sell + (Decimal(1) / buy)) / 2),
        )

    @property
    def tvl(self) -> Decimal:
        """Return total value locked for the order book."""
        if not self.buy_book_full or not self.sell_book_full:
            return Decimal(0)
        tvl = sum(b.quantity / b.price for b in self.buy_book_full) + sum(
            s.quantity * s.price for s in self.sell_book_full
        )
        return Decimal(int(tvl) / 10**6)

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = False,
    ) -> tuple[Assets, float]:
        """Get the amount of token output for the given input.

        SaturnSwap applies taker fees on output per-order, so fee handling is
        delegated to each order state.
        """
        if len(asset) != 1:
            raise ValueError("Asset should only have one token.")
        if asset.unit() not in [self.unit_a, self.unit_b]:
            raise ValueError(
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}",
            )

        if asset.unit() == self.unit_a:
            book = self.sell_book_full
            unit_out = self.unit_b
        else:
            book = self.buy_book_full
            unit_out = self.unit_a

        in_remaining = Assets(**{asset.unit(): int(asset.quantity())})
        out_assets = Assets(**{unit_out: 0})

        for order in book:
            state = order.state
            if state is None:
                continue

            order_out, _ = state.get_amount_out(in_remaining, precise=precise)
            order_in, _ = state.get_amount_in(order_out, precise=precise)

            if order_out.quantity() <= 0 or order_in.quantity() <= 0:
                break

            out_assets += order_out
            in_remaining -= order_in
            if in_remaining.quantity() <= 0:
                break

        return out_assets, 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = False,
    ) -> tuple[Assets, float]:
        """Get the amount of token input for the given output.

        SaturnSwap applies taker fees on output per-order, so fee handling is
        delegated to each order state.
        """
        if len(asset) != 1:
            raise ValueError("Asset should only have one token.")
        if asset.unit() not in [self.unit_a, self.unit_b]:
            raise ValueError(
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}",
            )

        if asset.unit() == self.unit_b:
            book = self.sell_book_full
            unit_in = self.unit_a
            unit_out = self.unit_b
        else:
            book = self.buy_book_full
            unit_in = self.unit_b
            unit_out = self.unit_a

        out_remaining = Assets(**{unit_out: int(asset.quantity())})
        in_assets = Assets(**{unit_in: 0})

        for order in book:
            state = order.state
            if state is None:
                continue

            max_out = state.available.quantity()
            take_out = min(out_remaining.quantity(), max_out)
            if take_out <= 0:
                continue

            order_out = Assets(**{unit_out: int(take_out)})
            order_in, _ = state.get_amount_in(order_out, precise=precise)
            if order_in.quantity() <= 0:
                break

            in_assets += order_in
            out_remaining -= order_out
            if out_remaining.quantity() <= 0:
                break

        return in_assets, 0

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
        """Build a transaction by walking the order book."""
        if in_assets.unit() == self.assets.unit():
            book = self.sell_book_full
        else:
            book = self.buy_book_full

        in_remaining = Assets.model_validate(in_assets.model_dump())
        txo = None
        datum = None

        for order in book:
            state = order.state
            if state is None:
                continue

            order_out, _ = state.get_amount_out(in_remaining)
            order_in, _ = state.get_amount_in(order_out)

            if order_out.quantity() <= 0 or order_in.quantity() <= 0:
                break

            txo, datum = state.swap_utxo(
                address_source=address_source,
                in_assets=order_in,
                out_assets=order_out,
                tx_builder=tx_builder,
            )

            in_remaining -= order_in
            if in_remaining.quantity() <= 0:
                break

        return txo, datum


def _ratio_amount(old_token_amount: int, new_token_amount: int, old_amount: int) -> int:
    """Ratio math using ceil rounding."""
    if old_token_amount == 0:
        return 0
    scale = 1_000_000_000_000
    ratio = (new_token_amount * scale + old_token_amount - 1) // old_token_amount
    return (old_amount * ratio + scale - 1) // scale
