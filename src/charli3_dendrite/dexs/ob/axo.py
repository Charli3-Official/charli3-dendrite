"""Axo Order Book Module."""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from typing import ClassVar
from typing import Union

import requests
from dotenv import load_dotenv
from pycardano import Address
from pycardano import AlonzoMetadata
from pycardano import AuxiliaryData
from pycardano import Metadata
from pycardano import MultiAsset
from pycardano import PlutusData
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import RedeemerTag
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano.utils import min_lovelace
from pydantic import BaseModel
from pydantic import field_validator

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import CancelRedeemer
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.utility import asset_to_value

formatter = logging.Formatter(
    fmt="%(asctime)s - %(name)-8s - %(levelname)-8s - %(message)s",
    datefmt="%d-%b-%y %H:%M:%S",
)
logger = logging.getLogger("cardem.api.dataclasses.axo")

load_dotenv()

AXO_API_KEY = os.environ.get("AXO_API_KEY")
HTTP_OK = 200
MIN_UTXO_VALUE = 1200000


@dataclass
class TimeMilliseconds(PlutusData):
    """Time in milliseconds for Plutus data."""

    CONSTR_ID = 7
    time_milliseconds: int


@dataclass
class Rationale(PlutusData):
    """Rationale for Plutus data."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class RationaleWrapper(PlutusData):
    """Wrapper for Rationale Plutus data."""

    CONSTR_ID = 2
    wrapper: Rationale


@dataclass
class AxoOrderDatum(OrderDatum):
    """Axo Order Datum."""

    CONSTR_ID = 0

    node_allocation: dict[int, dict[bytes, dict[bytes, int]]]
    asset_mapping: list[AssetClass]
    instance_token: AssetClass
    parameters: dict[bytes, Union[RationaleWrapper, TimeMilliseconds]]
    variables: dict[Any, Any]

    def address_source(self, block_time: int | None) -> str:
        """Get the address source."""
        if block_time is None:
            raise ValueError("Block time cannot be None")
        address = get_backend().get_axo_target(
            assets=self.instance_token.assets,
            block_time=datetime.fromtimestamp(block_time),
        )
        return Address.decode(address)

    def order_type(self) -> OrderType:
        """Get the order type."""
        return OrderType.swap

    def requested_amount(self) -> Assets:
        """Get the requested amount."""
        tokens = []
        for i, token in self.node_allocation.items():
            if len(token) == 0:
                tokens.append(0)
            else:
                tokens.append(
                    token[self.asset_mapping[i].policy][
                        self.asset_mapping[i].asset_name
                    ],
                )

        price: Rationale | None = None
        for _, value in self.parameters.items():
            if isinstance(value, RationaleWrapper):
                price = value.wrapper

        if price is None:
            raise ValueError("Could not find price")

        unit = (self.asset_mapping[1].policy + self.asset_mapping[1].asset_name).hex()
        if unit == "":
            unit = "lovelace"
        quantity = tokens[1] + tokens[0] * price.denominator // price.denominator
        return Assets(**{unit: quantity})


@dataclass
class AxoCancelRedeemer(PlutusData):
    """Axo Cancel Redeemer."""

    CONSTR_ID = 6


class AxoOBResponse(BaseModel):
    """Axo Order Book Response."""

    amount_unit: str
    amount_unit_ticker: str
    arrow_pair: str
    buy_side_amount: list[float]
    buy_side_depth: int | None = None
    buy_side_price: list[float]
    left: str
    left_ticker: str
    pair: str
    right: str
    right_ticker: str
    sell_side_amount: list[float]
    sell_side_depth: int | None = None
    sell_side_price: list[float]


class AxoCreateParams(BaseModel):
    """Axo Create Parameters."""

    left: str
    right: str
    amount: float | str
    start_date: datetime | None = None
    end_date: datetime | None = None
    price: float | str | None = None
    slippage: float | str = 3.0


class AxoCreateResponse(BaseModel):
    """Axo Create Response."""

    policy_script: str
    strat_id: str
    token_name: str
    datum: str
    algo_addr: str
    nft_metadata: dict

    @field_validator("nft_metadata", mode="before")
    @classmethod
    def validate_metadata(cls, v: str) -> dict:
        """Validate metadata."""
        return json.loads(v)

    @field_validator("policy_script")
    @classmethod
    def strip_policy(cls, v: str) -> str:
        """Trim the extra bytes cbor tag off it."""
        return v[6:]


class AxoCloseResponse(BaseModel):
    """Axo Close Response."""

    validator_address: str
    command_datum: str


class AxoCMCResponse(BaseModel):
    """Axo CMC Response."""

    base_currency: str
    base_subject: str
    base_volume: float
    highest_bid: float
    highest_price_24h: float | None
    last_price: float | None
    lowest_ask: float
    lowest_price_24h: float | None
    price_change_percent_24h: float | None
    quote_currency: str
    quote_subject: str
    quote_volume: float
    trading_pairs: str


class AxoAlgoName(Enum):
    """Axo Algorithm Names."""

    limit = "Limit"
    market = "Smart Market"
    dca = "DCA"


class AxoAPIClient:
    """Axo API Client."""

    def __init__(self) -> None:
        """Initialize Axo API Client."""
        self.headers = {"Content-Type": "application/json"}
        if AXO_API_KEY is not None:
            self.headers["x-api-key"] = AXO_API_KEY

    network = "mainnet"

    urls: ClassVar[dict[str, str]] = {
        "mainnet": "https://api.axo.trade/",
        "preprod": "https://api.axo-preview.trade/",
    }

    def cmc_summary(self) -> list[AxoCMCResponse]:
        """Get CMC summary."""
        url = self.urls[self.network] + "cmc/summary"

        result = requests.get(url, headers=self.headers, timeout=10)

        if result.status_code != HTTP_OK:
            raise ValueError(f"API request failed: {result.status_code}: {result.text}")

        return [AxoCMCResponse.model_validate(token) for token in result.json()]

    def aob(self, token_a: str, token_b: str) -> AxoOBResponse:
        """Get AOB."""
        url = self.urls[self.network] + "aob"

        token_a_param = "" if token_a == "lovelace" else token_a  # noqa: S105
        token_b_param = "" if token_b == "lovelace" else token_b  # noqa: S105

        result = requests.get(
            url,
            headers=self.headers,
            params={"left": token_a_param, "right": token_b_param},
            timeout=10,
        )

        if result.status_code != HTTP_OK:
            raise ValueError(f"API request failed: {result.status_code}: {result.text}")

        return AxoOBResponse.model_validate(result.json())

    def ob(self, token_a: str, token_b: str) -> AxoOBResponse:
        """Get OB."""
        url = self.urls[self.network] + "ob"

        token_a_param = "" if token_a == "lovelace" else token_a  # noqa: S105
        token_b_param = "" if token_b == "lovelace" else token_b  # noqa: S105

        result = requests.get(
            url,
            headers=self.headers,
            params={"left": token_a_param, "right": token_b_param},
            timeout=10,
        )

        if result.status_code != HTTP_OK:
            raise ValueError(f"API request failed: {result.status_code}: {result.text}")

        return AxoOBResponse.model_validate(result.json())

    def spot(self, token_a: str, token_b: str) -> float | None:
        """Get spot price."""
        url = self.urls[self.network] + "spot"

        token_a_param = "" if token_a == "lovelace" else token_a  # noqa: S105
        token_b_param = "" if token_b == "lovelace" else token_b  # noqa: S105

        result = requests.get(
            url,
            headers=self.headers,
            params={"left": token_a_param, "right": token_b_param},
            timeout=10,
        )

        if result.status_code != HTTP_OK:
            raise ValueError(f"API request failed: {result.status_code}: {result.text}")

        return result.json()

    def create(
        self,
        wallet_addr: str,
        tx_hash: str,
        tx_idx: int,
        params: AxoCreateParams,
        algo_name: AxoAlgoName = AxoAlgoName.market,
    ) -> AxoCreateResponse:
        """Create Axo order."""
        url = self.urls[self.network] + "create"

        result = requests.post(
            url,
            headers=self.headers,
            json={
                "wallet_addr": wallet_addr,
                "outref_utxo_id": tx_hash,
                "outref_utxo_ix": tx_idx,
                "algo_name": algo_name.value,
                "params": params.model_dump(exclude_none=True),
            },
            timeout=10,
        )

        if result.status_code != HTTP_OK:
            raise ValueError(f"API request failed: {result.status_code}: {result.text}")

        return AxoCreateResponse.model_validate(result.json())

    def notify(self, tx_hash: str, strat_id: str) -> requests.Response:
        """Notify Axo of transaction."""
        url = self.urls[self.network] + "notify"

        return requests.put(
            url,
            headers=self.headers,
            json={"tx_id": tx_hash, "strat_id": strat_id},
            timeout=10,
        )

    def close(
        self,
        wallet_address: str,
        return_address: str,
        strategy_id: str,
    ) -> AxoCloseResponse:
        """Close Axo order."""
        url = self.urls[self.network] + "close"

        result = requests.post(
            url,
            headers=self.headers,
            json={
                "wallet_address": wallet_address,
                "return_address": return_address,
                "strategy_id": strategy_id,
            },
            timeout=10,
        )

        if result.status_code != HTTP_OK:
            raise ValueError(f"API request failed: {result.status_code}: {result.text}")

        return AxoCloseResponse.model_validate(result.json())

    def get_ob_info(
        self,
        assets: Assets,
    ) -> tuple[AxoOBResponse, AxoOBResponse, float | None]:
        """Get order book info."""
        return (
            self.aob(
                token_a=assets.unit(0),
                token_b=assets.unit(1),
            ),
            self.ob(
                token_a=assets.unit(0),
                token_b=assets.unit(1),
            ),
            self.spot(
                token_a=assets.unit(0),
                token_b=assets.unit(1),
            ),
        )


class AxoOBMarketState(AbstractOrderBookState):
    """Axo Order Book Market State."""

    fee: int = 10
    spot: float = 1
    plutus_v2: bool = True
    inactive: bool = False
    _stake_address: ClassVar[Address] = Address.decode(
        "addr1z92l7rnra7sxjn5qv5fzc4fwsrrm29mgkleqj9a0y46j5lrryf9mtf9layje8u7u7wmap6alr28l90ry5t9nlyldjjsse4mxc9",
    )
    _client: ClassVar[AxoAPIClient] = AxoAPIClient()
    _reference_utxo: ClassVar[UTxO | None] = None
    _deposit: Assets = Assets(lovelace=8000000)

    @classmethod
    def dex(cls) -> str:
        """Get DEX name."""
        return "Axo"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return [
            "addr1z92l7rnra7sxjn5qv5fzc4fwsrrm29mgkleqj9a0y46j5lrryf9mtf9layje8u7u7wmap6alr28l90ry5t9nlyldjjsse4mxc9",
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        return []

    @property
    def swap_forward(self) -> bool:
        """Check if swap is forward."""
        return False

    @classmethod
    def default_script_class(cls) -> type[PlutusV2Script]:
        """Get default script class."""
        return PlutusV2Script

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Get reference UTxO."""
        if cls._reference_utxo is None:
            script_reference = get_backend().get_script_from_address(cls._stake_address)

            script = cls.default_script_class()(bytes.fromhex(script_reference.script))

            cls._reference_utxo = UTxO(
                input=TransactionInput(
                    transaction_id=TransactionId(
                        bytes.fromhex(
                            script_reference.tx_hash,
                        ),
                    ),
                    index=script_reference.tx_index,
                ),
                output=TransactionOutput(
                    address=Address.decode(script_reference.address),
                    amount=asset_to_value(script_reference.assets),
                    script=script,
                ),
            )
        return cls._reference_utxo

    @classmethod
    def _process_ob(
        cls,
        ob: AxoOBResponse,
    ) -> tuple[list[OrderBookOrder], list[OrderBookOrder]]:
        """Process order book response."""
        prices = get_token_prices(assets=[ob.left, ob.right])  # type: ignore # noqa: F821
        left = ob.left if ob.left != "" else "lovelace"
        if prices[0].policy_id + prices[0].policy_name == left:
            token_a_decimals = prices[0].decimals
            token_b_decimals = prices[1].decimals
        else:
            token_a_decimals = prices[1].decimals
            token_b_decimals = prices[0].decimals

        sell_book = []
        for index in range(len(ob.sell_side_price)):
            sell_book.append(
                OrderBookOrder(
                    price=ob.sell_side_price[index]
                    * 10 ** (token_a_decimals - token_b_decimals),
                    quantity=int(ob.sell_side_amount[index] * 10**token_b_decimals),
                ),
            )
        buy_book = []
        for index in range(len(ob.buy_side_price)):
            buy_book.append(
                OrderBookOrder(
                    price=ob.buy_side_price[index] ** -1
                    * 10 ** (token_b_decimals - token_a_decimals),
                    quantity=int(
                        ob.buy_side_amount[index]
                        * 10**token_a_decimals
                        * ob.buy_side_price[index],
                    ),
                ),
            )

        return BuyOrderBook(buy_book), SellOrderBook(sell_book)

    @classmethod
    def get_book(cls, assets: Assets) -> "AxoOBMarketState":
        """Get order book."""
        aob, ob, spot = cls._client.get_ob_info(assets)

        if spot is None:
            raise InvalidPoolError("Spot price is None")

        try:
            buy_book, sell_book = cls._process_ob(ob=aob)
            buy_book_full, sell_book_full = cls._process_ob(ob=ob)
        except IndexError as err:
            logger.error(f"Error getting Axo order book for assets: {assets}")
            raise InvalidPoolError("Error processing order book") from err

        if "lovelace" in assets:
            spot = 1.0

        return cls(
            assets=assets,
            spot=spot,
            block_time=int(datetime.now().timestamp()),
            block_index=0,
            buy_book_full=buy_book_full,
            sell_book_full=sell_book_full,
        )

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Get order datum class."""
        return AxoOrderDatum

    @property
    def stake_address(self) -> Address:
        """Get stake address."""
        return self._stake_address

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput, PlutusData, UTxO]:
        """Swap UTxO."""
        # Basic checks
        if len(in_assets) != 1 or len(out_assets) != 1:
            raise ValueError(
                "Only one asset can be supplied as input, "
                + "and one asset supplied as output.",
            )

        # Get the mint input UTxO
        utxo_input = None
        for utxo in tx_builder.inputs:
            if utxo.output.amount.coin > MIN_UTXO_VALUE:  # noqa: SIM102
                if utxo_input is None or len(utxo_input.output.to_cbor_hex()) < len(
                    utxo.output.to_cbor_hex(),
                ):
                    utxo_input = utxo

        if utxo_input is None:
            raise ValueError("No suitable input UTxO found")

        # Get the order build info
        prices = get_token_prices(assets=[in_assets.unit(), out_assets.unit()])  # type: ignore # noqa: F821
        if prices[0].policy_id + prices[0].policy_name == in_assets.unit():
            in_decimals = prices[0].decimals
        else:
            in_decimals = prices[1].decimals

        params = AxoCreateParams(
            left=in_assets.unit() if in_assets.unit() != "lovelace" else "",
            right=out_assets.unit() if out_assets.unit() != "lovelace" else "",
            amount=in_assets.quantity() / 10**in_decimals,
            slippage=self.slippage(in_assets=in_assets, out_assets=out_assets) + 2.0,
        )
        create: AxoCreateResponse = self._client.create(
            wallet_addr=address_source.encode(),
            tx_hash=utxo_input.input.transaction_id.payload.hex(),
            tx_idx=utxo_input.input.index,
            params=params,
        )

        # Create the mint metadata
        metadata = AuxiliaryData(
            AlonzoMetadata(metadata=Metadata({721: create.nft_metadata})),
        )
        tx_builder.auxiliary_data = metadata

        # Create the axo receipt
        axo_receipt = MultiAsset.from_primitive(
            {
                bytes.fromhex(create.strat_id): {
                    bytes.fromhex(create.token_name): 2,
                },
            },
        )
        tx_builder.mint = axo_receipt
        redeemer = Redeemer(CancelRedeemer())
        redeemer.tag = RedeemerTag.MINT
        tx_builder.add_minting_script(
            PlutusV2Script(bytes.fromhex(create.policy_script)),
            redeemer,
        )

        # Add in the recommended lovelace to the input and the axo receipt
        in_assets.root["lovelace"] = (
            in_assets["lovelace"]
            + self.batcher_fee(
                in_assets=in_assets,
                out_assets=out_assets,
                extra_assets=extra_assets,
            ).quantity()
            + self.deposit(in_assets=in_assets, out_assets=out_assets).quantity()
        )
        in_assets.root[create.strat_id + create.token_name] = 1

        # Create the swap utxo
        order_datum = AxoOrderDatum.from_cbor(create.datum)
        output = TransactionOutput(
            address=create.algo_addr,
            amount=asset_to_value(in_assets),
            datum=order_datum,
        )
        tx_builder.add_output(output)

        # Create the receipt UTxO
        utxo = TransactionOutput(
            address=address_source,
            amount=Value(
                coin=1000000,
                multi_asset=MultiAsset.from_primitive(
                    {
                        bytes.fromhex(create.strat_id): {
                            bytes.fromhex(create.token_name): 1,
                        },
                    },
                ),
            ),
        )
        utxo.amount.coin = min_lovelace(context=tx_builder.context, output=utxo)
        tx_builder.add_output(utxo)

        return output, order_datum, utxo_input

    @property
    def volume_fee(self) -> int:
        """Get volume fee."""
        return 10

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        """Get cancel redeemer."""
        return Redeemer(AxoCancelRedeemer())

    def batcher_fee(
        self,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
    ) -> Assets:
        """Batcher fee.

        Args:
            in_assets: The input assets for the swap
            out_assets: The output assets for the swap
            extra_assets: Extra assets included in the transaction
        """
        if in_assets.unit() == "lovelace":
            fees = max(self.volume_fee * in_assets.quantity() // 10000, 1200000)
        elif out_assets.unit() == "lovelace":
            fees = max(self.volume_fee * out_assets.quantity() // 10000, 1200000)
        else:
            fees = max(
                self.volume_fee * in_assets.quantity() * self.spot // 10000,
                1200000,
            )

        # The below code estimates the Cardano cost of executing the tx
        fees += 250000  # ~cost of output tx

        if in_assets.unit() == self.unit_a:
            book = self.sell_book_full
        else:
            book = self.buy_book_full

        # Each fill order incurs ~0.6 ada cost
        index = 0
        in_quantity = in_assets.quantity()
        while in_quantity > 0 and index < len(book):
            available = book[index].quantity * book[index].price
            fees += 600000
            if available > in_quantity:
                in_quantity = 0
            else:
                in_quantity -= book[index].price * book[index].quantity
            index += 1

        return Assets(lovelace=fees)

    def slippage(
        self,
        in_assets: Assets,
        out_assets: Assets,
    ) -> float:
        """Calculate slippage.

        Args:
            in_assets: The input assets for the swap
            out_assets: The output assets for the swap
        """
        if in_assets.unit() == "lovelace":
            fees = max(self.volume_fee * in_assets.quantity() // 10000, 1200000)
        elif out_assets.unit() == "lovelace":
            fees = max(self.volume_fee * out_assets.quantity() // 10000, 1200000)
        else:
            fees = max(
                self.volume_fee * in_assets.quantity() * self.spot // 10000,
                1200000,
            )

        # The below code estimates the Cardano cost of executing the tx
        fees += 250000  # ~cost of output tx

        if in_assets.unit() == self.unit_a:
            book = self.sell_book_full
        else:
            book = self.buy_book_full

        # Each fill order incurs ~0.5 ada cost
        index = 0
        best_price = book[index].price if book else 0
        in_quantity = in_assets.quantity()
        last_price = best_price
        while in_quantity > 0 and index < len(book):
            available = book[index].quantity * book[index].price
            fees += 500000
            if available > in_quantity:
                in_quantity = 0
            else:
                in_quantity -= book[index].price * book[index].quantity
            last_price = book[index].price
            index += 1

        return 100 * abs(1 - (best_price / last_price)) if last_price != 0 else 0

    @property
    def pool_id(self) -> str:
        """Get the pool ID."""
        return f"{self.dex}.{self.unit_a}.{self.unit_b}"
