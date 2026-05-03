"""ChadSwap Order Book Module.

ChadSwap is an order-book style DEX on Cardano that supports both buy and sell
limit orders with optional partial fills.
"""

from dataclasses import dataclass
from decimal import Decimal
from hashlib import blake2b
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV3Script
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
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderState
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.utility import asset_to_value


@dataclass
class ChadSwapTakeRedeemer(PlutusData):
    """Redeemer for taking/filling an order."""

    CONSTR_ID = 0
    index: int  # Index of the maker output


@dataclass
class ChadSwapCancelRedeemer(PlutusData):
    """Redeemer for canceling an order."""

    CONSTR_ID = 1
    index: int  # Index of the fees output


@dataclass
class ChadSwapSell(PlutusData):
    """Sell order type - maker is selling tokens for ADA."""

    CONSTR_ID = 0


@dataclass
class ChadSwapBuy(PlutusData):
    """Buy order type - maker is buying tokens with ADA."""

    CONSTR_ID = 1


@dataclass
class ChadSwapPlutusFalse(PlutusData):
    """Aiken False - Constr(0, [])."""

    CONSTR_ID = 0


@dataclass
class ChadSwapSomeInt(PlutusData):
    """Some(Int) wrapper for optional integer values."""

    CONSTR_ID = 0
    value: int


@dataclass
class ChadSwapSomeOutputId(PlutusData):
    """Some(OutputId) wrapper for optional output ID values."""

    CONSTR_ID = 0
    value: bytes


@dataclass
class ChadSwapOutputReference(PlutusData):
    """Output reference used to compute OutputId.

    Matches the Aiken `OutputReference` (Constr 0 [tx_id, index]).
    """

    CONSTR_ID = 0
    tx_id: bytes
    index: int


@dataclass
class ChadSwapDatumState(PlutusData):
    """Current fill state of an order.

    This tracks how much of the order has been filled across partial fills.
    """

    CONSTR_ID = 0
    tokens_left: int  # Amount of tokens remaining to be filled
    tokens_filled: int  # Amount of tokens already filled


@dataclass
class ChadSwapConfig(PlutusData):
    """Immutable order configuration set when order is created.

    Fields:
        maker: Order creator's address (receives funds when filled)
        order_type: Sell or Buy
        policy_id: Token policy ID being traded
        asset_name: Token asset name being traded
        unit_price: Price per token in lovelace
        unit_price_denom: Optional denominator for sub-lovelace prices
        allow_partial_fills: Whether partial fills are allowed
        expiration: Optional POSIX time in milliseconds
    """

    CONSTR_ID = 0
    maker: PlutusFullAddress
    order_type: Union[ChadSwapSell, ChadSwapBuy]
    policy_id: bytes
    asset_name: bytes
    unit_price: int
    unit_price_denom: Union[PlutusNone, ChadSwapSomeInt]
    allow_partial_fills: Union[ChadSwapPlutusFalse, PlutusNone]
    expiration: Union[PlutusNone, ChadSwapSomeInt]


@dataclass
class ChadSwapDatum(OrderDatum):
    """ChadSwap order datum containing config, state, and tracking fields.

    Fields:
        config: Immutable order configuration
        state: Current fill state (mutable across partial fills)
        tag: Output ID of the order this partially fills (partial fills only)
        order_id: Output ID of the original order (partial fills only)
    """

    CONSTR_ID = 0
    config: ChadSwapConfig
    state: ChadSwapDatumState
    tag: Union[PlutusNone, ChadSwapSomeOutputId]
    order_id: Union[PlutusNone, ChadSwapSomeOutputId]

    def pool_pair(self) -> Assets | None:
        """Return the token pair for this order (ADA + traded token)."""
        token_unit = self.config.policy_id.hex() + self.config.asset_name.hex()
        return Assets(lovelace=0) + Assets(**{token_unit: 0})

    def address_source(self) -> str | None:
        """Return the maker's address as bech32 string."""
        return self.config.maker.to_address().encode()

    def requested_amount(self) -> Assets:
        """Return the requested amount based on order type."""
        token_unit = self.config.policy_id.hex() + self.config.asset_name.hex()

        if isinstance(self.config.order_type, ChadSwapSell):
            # Sell order: maker wants ADA for their tokens
            unit_price = self.config.unit_price
            if isinstance(self.config.unit_price_denom, ChadSwapSomeInt):
                ada_amount = (
                    self.state.tokens_left * unit_price
                ) // self.config.unit_price_denom.value
            else:
                ada_amount = self.state.tokens_left * unit_price
            return Assets(lovelace=ada_amount)

        # Buy order: maker wants tokens for their ADA
        return Assets(**{token_unit: self.state.tokens_left})

    def order_type(self) -> OrderType | None:
        """Return the order type classification."""
        # Both buy and sell are swap operations
        return OrderType.swap


class ChadSwapOrderState(AbstractOrderState):
    """ChadSwap order state for individual orders on the order book."""

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False

    _batcher: Assets = Assets(lovelace=0)
    _datum_parsed: PlutusData | None = None

    @classmethod
    def dex(cls) -> str:
        """Official DEX name."""
        return "ChadSwap"

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """The DEX NFT policy - ChadSwap does not utilize order NFTs."""
        return None

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information - script addresses."""
        return ["addr1w84q0y2wwfj5efd9ch3x492edeh6pdwycvt7g030jfzhagg5ftr54"]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool/order selection criteria."""
        return PoolSelector(addresses=cls.order_selector())

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """The PlutusData class for parsing order datums."""
        return ChadSwapDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV3Script]:
        """Default Plutus script version for ChadSwap."""
        return PlutusV3Script

    @property
    def price(self) -> tuple[int, int]:
        """Price as (numerator, denominator) for in_unit/out_unit."""
        datum = self.order_datum
        numerator = datum.config.unit_price

        if isinstance(datum.config.unit_price_denom, ChadSwapSomeInt):
            denominator = datum.config.unit_price_denom.value
        else:
            denominator = 1

        if isinstance(datum.config.order_type, ChadSwapBuy):
            return (denominator, numerator)

        return (numerator, denominator)

    @property
    def available(self) -> Assets:
        """Max amount of output asset that can be used to fill the order.

        Sell orders: remaining tokens offered (tokens_left).
        Buy orders: remaining ADA offered (derived from tokens_left * price).
        """
        datum = self.order_datum
        tokens_left = datum.state.tokens_left

        if isinstance(datum.config.order_type, ChadSwapSell):
            token_unit = datum.config.policy_id.hex() + datum.config.asset_name.hex()
            return Assets(**{token_unit: tokens_left})

        denom = (
            datum.config.unit_price_denom.value
            if isinstance(
                datum.config.unit_price_denom,
                ChadSwapSomeInt,
            )
            else 1
        )
        return Assets(lovelace=(tokens_left * datum.config.unit_price) // denom)

    @property
    def volume_fee(self) -> int:
        """Fee percentage in basis points.

        Chadswap fees are paid by the maker, so no fee applied for order fills.
        """
        return 0

    @property
    def reference_utxo(self) -> UTxO | None:
        """Get the script reference UTxO.

        Returns None if no reference script is available.
        """
        script_info = get_backend().get_script_from_address(
            Address.decode(
                "addr1w84q0y2wwfj5efd9ch3x492edeh6pdwycvt7g030jfzhagg5ftr54",
            ),
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
                script=PlutusV3Script(bytes.fromhex(script_info.script)),
            ),
        )

    @property
    def tvl(self) -> int:
        """Total value locked in the order."""
        return self.available

    @property
    def pool_id(self) -> str:
        """Unique identifier for the pool or ob."""
        datum = self.order_datum
        return datum.config.policy_id.hex() + datum.config.asset_name.hex()

    @property
    def swap_forward(self) -> bool:
        """Whether swaps go forward through this order."""
        return True

    @property
    def stake_address(self) -> Address | None:
        """Stake address for the order, if any."""
        return None

    @classmethod
    def post_init(cls, values: dict) -> dict:
        """Post-initialization checks and modifications.

        Reorders assets for buy orders so that in_unit/out_unit align correctly,
        checks expiration, and validates the order state.
        """
        super().post_init(values)

        datum = cls.order_datum_class().from_cbor(values["datum_cbor"])

        if isinstance(datum.config.order_type, ChadSwapBuy):
            # For buy orders the taker sends tokens and receives ADA, so
            # token must be first (in_unit) and lovelace second (out_unit).
            lovelace_qty = values["assets"].root.pop("lovelace")
            values["assets"].root["lovelace"] = lovelace_qty

        if isinstance(datum.config.expiration, ChadSwapSomeInt):
            import time

            expiration_ms = datum.config.expiration.value
            now_ms = int(time.time() * 1000)
            if now_ms >= expiration_ms:
                values["inactive"] = True

        if datum.state.tokens_left <= 0:
            values["inactive"] = True

        return values

    @classmethod
    def skip_init(cls, values: dict) -> bool:  # noqa: ARG003
        """Determine if initialization should be skipped.

        Skip if datum cannot be parsed or order is clearly invalid.
        """
        return False

    @staticmethod
    def _ensure_min_lovelace(
        tx_builder: TransactionBuilder,
        output: TransactionOutput,
    ) -> TransactionOutput:
        """Ensure output contains at least chain-required minimum lovelace."""
        min_ada = min_lovelace(tx_builder.context, output=output)
        if output.amount.coin < min_ada:
            output.amount.coin = min_ada
        return output

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
        """Build a transaction to take/fill this order.

        Args:
            address_source: The taker's address
            in_assets: Assets being provided by the taker
            out_assets: Assets the taker wants to receive
            tx_builder: Transaction builder to add inputs/outputs to
            extra_assets: Additional assets to include
            address_target: Target address for output (defaults to address_source)
            datum_target: Datum for the output

        Returns:
            Tuple of (output, datum) for the transaction
        """
        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            addresses=self.pool_selector().addresses,
        )

        input_assets = self.assets.copy()

        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(input_assets),
                datum_hash=self.order_datum.hash(),
            ),
        )

        is_partial = out_assets.quantity() < self.available.quantity()

        partial_allowed = isinstance(
            self.order_datum.config.allow_partial_fills,
            PlutusNone,
        )
        if is_partial and not partial_allowed:
            raise ValueError("Partial fills not allowed for this order")

        output_index = len(tx_builder.outputs)

        redeemer = Redeemer(ChadSwapTakeRedeemer(index=output_index))

        tx_builder.add_script_input(
            utxo=input_utxo,
            script=self.reference_utxo,
            redeemer=redeemer,
        )

        tx_builder.datums.update({self.order_datum.hash(): self.order_datum})

        datum = self.order_datum
        maker_address = datum.config.maker.to_address()
        output_ref = ChadSwapOutputReference(
            tx_id=bytes.fromhex(self.tx_hash),
            index=self.tx_index,
        )
        output_id_bytes = blake2b(
            output_ref.to_cbor(),
            digest_size=32,
        ).digest()

        if is_partial:
            new_state = ChadSwapDatumState(
                tokens_left=datum.state.tokens_left - out_assets.quantity(),
                tokens_filled=datum.state.tokens_filled + out_assets.quantity(),
            )

            new_datum = ChadSwapDatum(
                config=datum.config,
                state=new_state,
                tag=ChadSwapSomeOutputId(value=output_id_bytes),
                order_id=(
                    datum.order_id
                    if isinstance(datum.order_id, ChadSwapSomeOutputId)
                    else ChadSwapSomeOutputId(value=output_id_bytes)
                ),
            )

            residual_assets = input_assets.copy()
            residual_assets.root[out_assets.unit()] -= out_assets.quantity()
            residual_output = TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(residual_assets),
                datum_hash=new_datum.hash(),
            )
            residual_output = self._ensure_min_lovelace(tx_builder, residual_output)
            tx_builder.datums.update({new_datum.hash(): new_datum})

            num, denom = self.price
            if isinstance(datum.config.order_type, ChadSwapSell):
                # Sell order: maker receives ADA
                maker_receives = (out_assets.quantity() * num) // denom
                maker_output = TransactionOutput(
                    address=maker_address,
                    amount=asset_to_value(Assets(lovelace=maker_receives)),
                    datum=output_id_bytes,
                )
            else:
                # Buy order: maker receives tokens
                maker_output = TransactionOutput(
                    address=maker_address,
                    amount=asset_to_value(out_assets),
                    datum=output_id_bytes,
                )
            maker_output = self._ensure_min_lovelace(tx_builder, maker_output)

            tx_builder.add_output(maker_output)
            return residual_output, new_datum

        num, denom = self.price
        if isinstance(datum.config.order_type, ChadSwapSell):
            # Sell order: maker receives all the ADA
            total_ada = (datum.state.tokens_left * num) // denom
            maker_output = TransactionOutput(
                address=maker_address,
                amount=asset_to_value(Assets(lovelace=total_ada)),
                datum=output_id_bytes,
            )
        else:
            # Buy order: maker receives all the tokens
            token_unit = datum.config.policy_id.hex() + datum.config.asset_name.hex()
            maker_output = TransactionOutput(
                address=maker_address,
                amount=asset_to_value(Assets(**{token_unit: datum.state.tokens_left})),
                datum=output_id_bytes,
            )
        maker_output = self._ensure_min_lovelace(tx_builder, maker_output)

        return maker_output, datum

    def cancel_utxo(
        self,
        address_source: Address,
        tx_builder: TransactionBuilder,
    ) -> tuple[TransactionOutput, PlutusData]:
        """Build a transaction to cancel this order.

        Only the maker can cancel their own order.

        Args:
            address_source: The maker's address (must match order maker)
            tx_builder: Transaction builder to add inputs/outputs to

        Returns:
            Tuple of (output, datum) returning funds to maker
        """
        datum = self.order_datum
        maker_address = datum.config.maker.to_address()

        # Verify the canceller is the maker
        if address_source.encode() != maker_address.encode():
            raise ValueError("Only the order maker can cancel the order")

        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            addresses=self.pool_selector().addresses,
        )

        input_assets = self.assets.copy()

        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(input_assets),
                datum_hash=datum.hash(),
            ),
        )

        # Fee output index
        fee_output_index = len(tx_builder.outputs)

        redeemer = Redeemer(ChadSwapCancelRedeemer(index=fee_output_index))

        tx_builder.add_script_input(
            utxo=input_utxo,
            script=self.reference_utxo,
            redeemer=redeemer,
        )

        tx_builder.datums.update({datum.hash(): datum})

        # Return assets to maker
        maker_output = TransactionOutput(
            address=maker_address,
            amount=asset_to_value(input_assets),
        )
        maker_output = self._ensure_min_lovelace(tx_builder, maker_output)

        return maker_output, datum


class ChadSwapOrderBook(AbstractOrderBookState):
    """ChadSwap order book aggregating individual orders for a token pair."""

    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def get_book(
        cls,
        assets: Assets,
        orders: list[ChadSwapOrderState] | None = None,
    ) -> "ChadSwapOrderBook":
        """Build an order book from ChadSwap orders for the given token pair.

        Args:
            assets: The token pair to build the book for (lovelace + token).
            orders: Pre-fetched orders, or None to fetch from the backend.
        """
        if orders is None:
            selector = ChadSwapOrderState.pool_selector()

            result = get_backend().get_pool_utxos(
                limit=10000,
                historical=False,
                **selector.model_dump(),
            )

            orders = [ChadSwapOrderState.model_validate(r.model_dump()) for r in result]

        buy_orders = []
        sell_orders = []
        for order in orders:
            if order.inactive:
                continue
            num, denom = order.price
            price = num / denom
            o = OrderBookOrder(
                price=price,
                quantity=int(order.available.quantity()),
                state=order,
            )
            if order.in_unit == assets.unit() and order.out_unit == assets.unit(1):
                sell_orders.append(o)
            elif order.in_unit == assets.unit(1) and order.out_unit == assets.unit(0):
                buy_orders.append(o)

        import time

        return ChadSwapOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook(sell_orders),
            buy_book_full=BuyOrderBook(buy_orders),
        )

    @classmethod
    def dex(cls) -> str:
        """Official DEX name."""
        return "ChadSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return ChadSwapOrderState.order_selector()

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool/order selection criteria."""
        return ChadSwapOrderState.pool_selector()

    @classmethod
    def default_script_class(cls) -> type[PlutusV3Script]:
        """Default Plutus script version."""
        return ChadSwapOrderState.default_script_class()

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """The PlutusData class for parsing order datums."""
        return ChadSwapOrderState.order_datum_class()

    @property
    def swap_forward(self) -> bool:
        """Whether swap forwarding is enabled."""
        return False

    @property
    def stake_address(self) -> Address | None:
        """Stake address for the order book."""
        return None

    @property
    def pool_id(self) -> str:
        """Unique identifier for the ChadSwap order book."""
        return "ChadSwap"

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Mid price of assets based on the full order books."""
        if not self.buy_book_full or not self.sell_book_full:
            return Decimal(0), Decimal(0)
        buy = Decimal(self.buy_book_full[0].price)
        sell = Decimal(self.sell_book_full[0].price)
        return (
            Decimal((buy + (Decimal(1) / sell)) / 2),
            Decimal((sell + (Decimal(1) / buy)) / 2),
        )

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
        """Build a transaction by walking the order book.

        Iterates over orders in the appropriate book (sell or buy),
        filling each until the input is exhausted.
        """
        if in_assets.unit() == self.assets.unit():
            book = self.sell_book_full
        else:
            book = self.buy_book_full

        in_remaining = Assets.model_validate(in_assets.model_dump())
        txo = None
        datum = None

        for order in book:
            state = order.state

            order_out, _ = state.get_amount_out(in_remaining)
            order_in, _ = state.get_amount_in(order_out)

            # Stop if remaining budget cannot satisfy the smallest meaningful fill.
            if order_out.quantity() <= 0 or order_in.quantity() <= 0:
                break

            txo, datum = state.swap_utxo(
                address_source=address_source,
                in_assets=order_in,
                out_assets=order_out,
                tx_builder=tx_builder,
            )

            in_remaining -= order_in
            if in_remaining.quantity() <= state.price[0] / state.price[1]:
                break

        return txo, datum
