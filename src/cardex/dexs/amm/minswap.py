"""Data classes and utilities for Minswap Dex.

This contains data classes and utilities for handling various order and pool datums
"""
from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.datums import PoolDatum
from cardex.dataclasses.datums import ReceiverDatum
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractCommonStableSwapPoolState
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.core.constants import ONE_VALUE
from cardex.dexs.core.constants import TWO_VALUE
from cardex.utility import Assets


@dataclass
class SwapExactIn(PlutusData):
    """Swap exact in order datum."""

    CONSTR_ID = 0
    desired_coin: AssetClass
    minimum_receive: int

    @classmethod
    def from_assets(cls, asset: Assets) -> "SwapExactIn":
        """Parse an Assets object into a SwapExactIn datum."""
        if len(asset) != ONE_VALUE:
            error_msg = "Asset should only have one token"
            raise ValueError(error_msg)
        return SwapExactIn(
            desired_coin=AssetClass.from_assets(asset),
            minimum_receive=asset.quantity(),
        )


@dataclass
class StableSwapExactIn(PlutusData):
    """Swap exact in order datum."""

    CONSTR_ID = 0
    input_coin: int
    output_coin: int
    minimum_receive: int

    @classmethod
    def from_assets(cls, in_assets: Assets, out_assets: Assets) -> "StableSwapExactIn":
        """Parse an Assets object into a SwapExactIn datum."""
        if len(in_assets) != ONE_VALUE:
            error_msg = "in_assets should only have one token"
            raise ValueError(error_msg)
        if len(out_assets) != ONE_VALUE:
            error_msg = "out_assets should only have one token"
            raise ValueError(error_msg)
        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            input_coin = 0
            output_coin = 1
        else:
            input_coin = 1
            output_coin = 0

        return StableSwapExactIn(
            input_coin=input_coin,
            output_coin=output_coin,
            minimum_receive=out_assets.quantity(),
        )


@dataclass
class StableSwapDeposit(PlutusData):
    """Swap exact out order datum."""

    CONSTR_ID = 1
    expected_receive: int

    @classmethod
    def from_assets(cls, asset: Assets) -> "StableSwapDeposit":
        """Parse an Assets object into a SwapExactOut datum."""
        if len(asset) != ONE_VALUE:
            error_msg = "Asset should only have one token"
            raise ValueError(error_msg)

        return StableSwapDeposit(
            expected_receive=asset.quantity(),
        )


@dataclass
class StableSwapWithdraw(PlutusData):
    """Swap exact out order datum."""

    CONSTR_ID = 2
    expected_receive: list[int]

    @classmethod
    def from_assets(cls, asset: Assets) -> "StableSwapWithdraw":
        """Parse an Assets object into a SwapExactOut datum."""
        if len(asset) != TWO_VALUE:
            error_msg = "Asset should have two tokens"
            raise ValueError(error_msg)

        return StableSwapWithdraw(
            expected_receive=[asset.quantity(), asset.quantity(1)],
        )


@dataclass
class StableSwapWithdrawOneCoin(PlutusData):
    """Swap exact out order datum."""

    CONSTR_ID = 4
    expected_receive: Any

    @classmethod
    def from_assets(cls, coin_index: int, asset: Assets) -> "StableSwapWithdrawOneCoin":
        """Parse an Assets object into a SwapExactOut datum."""
        if len(asset) != ONE_VALUE:
            error_msg = "Asset should only have one token"
            raise ValueError(error_msg)

        return StableSwapWithdrawOneCoin(
            expected_receive=[coin_index, asset.quantity()],
        )


@dataclass
class SwapExactOut(PlutusData):
    """Swap exact out order datum."""

    CONSTR_ID = 1
    desired_coin: AssetClass
    expected_receive: int

    @classmethod
    def from_assets(cls, asset: Assets) -> "SwapExactOut":
        """Parse an Assets object into a SwapExactOut datum."""
        if len(asset) != ONE_VALUE:
            error_msg = "Asset should only have one token"
            raise ValueError(error_msg)

        return SwapExactOut(
            desired_coin=AssetClass.from_assets(asset),
            expected_receive=asset.quantity(),
        )


@dataclass
class Deposit(PlutusData):
    """Swap exact in order datum."""

    CONSTR_ID = 2
    minimum_lp: int


@dataclass
class Withdraw(PlutusData):
    """Swap exact in order datum."""

    CONSTR_ID = 3
    min_asset_a: int
    min_asset_b: int


@dataclass
class ZapIn(PlutusData):
    """Swap exact in order datum."""

    CONSTR_ID = 4
    desired_coin: AssetClass
    minimum_lp: int


@dataclass
class MinswapOrderDatum(OrderDatum):
    """An order datum."""

    sender: PlutusFullAddress
    receiver: PlutusFullAddress
    receiver_datum_hash: Union[ReceiverDatum | PlutusNone]
    step: Union[
        SwapExactIn,
        SwapExactOut,
        Deposit,
        Withdraw,
        ZapIn,
        StableSwapExactIn,
        StableSwapDeposit,
        StableSwapWithdraw,
        StableSwapWithdrawOneCoin,
    ]
    batcher_fee: int
    deposit: int

    @classmethod
    def create_datum(  # noqa: PLR0913
        cls,
        address_source: Address,
        in_assets: Assets,  # noqa: ARG003
        out_assets: Assets,
        batcher_fee: Assets,
        deposit: Assets,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> "MinswapOrderDatum":
        """Create a Minswap order datum.

        Args:
            address_source: Source address for the order.
            in_assets: Input assets for the order.
            out_assets: Output assets for the order.
            batcher_fee: Batcher fee for the order.
            deposit: Deposit amount for the order.
            address_target: Target address for the order (optional).
            datum_target: Target datum for the order (optional).

        Returns:
            MinswapOrderDatum: Constructed order datum instance.
        """
        full_address_source = PlutusFullAddress.from_address(address_source)
        step = SwapExactIn.from_assets(out_assets)

        if address_target is None:
            address_target = address_source
            datum_target = PlutusNone()
        elif datum_target is None:
            datum_target = PlutusNone()

        full_address_target = PlutusFullAddress.from_address(address_target)

        return cls(
            full_address_source,
            full_address_target,
            datum_target,
            step,
            batcher_fee.quantity(),
            deposit.quantity(),
        )

    def address_source(self) -> str:
        """Returns the source address of the sender."""
        if self.sender.to.to_address() is None:
            error_msg = "None"
            raise ValueError(error_msg)
        return self.sender.to_address()

    def requested_amount(self) -> Assets:
        """Returns the requested amount based on the order type."""
        if isinstance(self.step, SwapExactIn):
            return Assets(
                {self.step.desired_coin.assets.unit(): self.step.minimum_receive},
            )
        if isinstance(self.step, SwapExactOut):
            return Assets(
                {self.step.desired_coin.assets.unit(): self.step.expected_receive},
            )
        if isinstance(self.step, Deposit):
            return Assets({"lp": self.step.minimum_lp})
        if isinstance(self.step, Withdraw):
            return Assets(
                {"asset_a": self.step.min_asset_a, "asset_b": self.step.min_asset_a},
            )
        if isinstance(self.step, ZapIn):
            return Assets({self.step.desired_coin.assets.unit(): self.step.minimum_lp})
        raise ValueError

    def order_type(self) -> OrderType:
        """Returns the type of order (swap, deposit, withdraw, zap_in)."""
        if isinstance(self.step, (SwapExactIn, SwapExactOut, StableSwapExactIn)):
            return OrderType.swap
        if isinstance(self.step, (Deposit, StableSwapDeposit)):
            return OrderType.deposit
        if isinstance(
            self.step,
            (Withdraw, StableSwapWithdraw, StableSwapWithdrawOneCoin),
        ):
            return OrderType.withdraw
        if isinstance(self.step, ZapIn):
            return OrderType.zap_in
        return None


@dataclass
class MinswapStableOrderDatum(MinswapOrderDatum):
    """A stable order datum for Minswap."""

    @classmethod
    def create_datum(  # noqa: PLR0913
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,
        deposit: Assets,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> "MinswapStableOrderDatum":
        """Create a Minswap stable order datum.

        Args:
            address_source: Source address for the order.
            in_assets: Input assets for the order.
            out_assets: Output assets for the order.
            batcher_fee: Batcher fee for the order.
            deposit: Deposit amount for the order.
            address_target: Target address for the order (optional).
            datum_target: Target datum for the order (optional).

        Returns:
            MinswapStableOrderDatum: Constructed stable order datum instance.
        """
        full_address_source = PlutusFullAddress.from_address(address_source)
        step = StableSwapExactIn.from_assets(in_assets=in_assets, out_assets=out_assets)

        if address_target is None:
            address_target = address_source
            datum_target = PlutusNone()
        elif datum_target is None:
            datum_target = PlutusNone()

        full_address_target = PlutusFullAddress.from_address(address_target)

        return cls(
            full_address_source,
            full_address_target,
            datum_target,
            step,
            batcher_fee.quantity(),
            deposit.quantity(),
        )


@dataclass
class FeeDatumHash(PlutusData):
    """Fee datum hash."""

    CONSTR_ID = 0
    fee_hash: bytes


@dataclass
class FeeSwitchOn(PlutusData):
    """Fee switch on."""

    CONSTR_ID = 0
    address: PlutusFullAddress
    fee_to_datum_hash: PlutusNone


@dataclass
class _FeeSwitchWrapper(PlutusData):
    """Fee switch wrapper."""

    CONSTR_ID = 0
    fee_sharing: FeeSwitchOn


@dataclass
class MinswapPoolDatum(PoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    asset_a: AssetClass
    asset_b: AssetClass
    total_liquidity: int
    root_k_last: int
    fee_sharing: Union[_FeeSwitchWrapper, PlutusNone]

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the pool."""
        return self.asset_a.assets + self.asset_b.assets


@dataclass
class MinswapStablePoolDatum(PlutusData):
    """Stable Pool Datum."""

    CONSTR_ID = 0

    balances: list[int]
    total_liquidity: int
    amp: int
    order_hash: bytes

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the pool (Not Implemented)."""
        raise NotImplementedError


@dataclass
class MinswapDJEDiUSDStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the DJEDiUSD stable pool."""
        return Assets(
            **{
                "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344": 0,
                "f66d78b4a3cb3d37afa0ec36461e51ecbde00f26c8f0a68f94b6988069555344": 0,
            },
        )


@dataclass
class MinswapDJEDUSDCStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        """Return the asset pair associated with the pool."""
        return Assets(
            **{
                "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344": 0,
                "25c5de5f5b286073c593edfd77b48abc7a48e5a4f3d4cd9d428ff93555534443": 0,
            },
        )


@dataclass
class MinswapDJEDUSDMStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the DJEDUSDM stable pool."""
        return Assets(
            **{
                "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344": 0,
                "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d": 0,
            },
        )


class MinswapCPPState(AbstractConstantProductPoolState):
    """Represents the state of a constant product pool for Minswap."""

    fee: int = 30
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1zxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uw6j2c79gy9l76sdg0xwhd7r0c0kna0tycz4y5s6mlenh8pq6s3z70",
        ),
        Address.from_primitive(
            "addr1wxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uwc0h43gt",
        ),
    ]

    @classmethod
    def dex(cls) -> str:
        """Returns the name of the DEX."""
        return "Minswap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Returns the order selectors."""
        return [s.encode() for s in cls._stake_address]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector."""
        return PoolSelector(
            selector_type="assets",
            selector=[
                "13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f4d494e53574150",
            ],
        )

    @property
    def swap_forward(self) -> bool:
        """Returns whether the swap direction is forward."""
        return True

    @property
    def stake_address(self) -> Address:
        """Returns the stake address."""
        return self._stake_address[0]

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns the class type of order datum."""
        return MinswapOrderDatum

    @classmethod
    def script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the script class."""
        return PlutusV1Script

    @classmethod
    def pool_datum_class(cls) -> type[MinswapPoolDatum]:
        """Returns the class type of pool datum."""
        return MinswapPoolDatum

    def batcher_fee(
        self,
        in_assets: Assets | None = None,  # noqa: ARG002
        out_assets: Assets | None = None,  # noqa: ARG002
        extra_assets: Assets | None = None,
    ) -> Assets:
        """Batcher fee.

        For Minswap, the batcher fee decreases linearly from 2.0 ADA to 1.5 ADA as the
        MIN in the input assets from 0 - 50,000 MIN.
        """
        min_addr = "29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c64d494e"
        if extra_assets is not None and min_addr in extra_assets:
            fee_reduction = min(extra_assets[min_addr] // 10**5, 500000)
        else:
            fee_reduction = 0
        return self._batcher - Assets(lovelace=fee_reduction)

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        if self.pool_nft is None:
            error_msg = "pool_nft is None"
            raise ValueError(error_msg)
        return self.pool_nft.unit()

    @classmethod
    def pool_policy(cls) -> list[str]:
        """Returns pool policy."""
        return ["0be55d262b29f564998ff81efe21bdc0022621c12f15af08d0f2ddb1"]

    @classmethod
    def lp_policy(cls) -> list[str]:
        """Returns lp policy."""
        return ["e4214b7cce62ac6fbba385d164df48e157eae5863521b4b67ca71d86"]

    @classmethod
    def dex_policy(cls) -> list[str]:
        """Returns dex policy."""
        return ["13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f"]


class MinswapDJEDiUSDStableState(AbstractCommonStableSwapPoolState, MinswapCPPState):
    """Represents the state of the DJEDiUSD stable pool in Minswap.

    Attributes:
        fee (float): The fee percentage.
        _batcher (Assets): The batcher assets.
        _deposit (Assets): The deposit assets.
        _stake_address (ClassVar[Address]): The stake addresses.
    """

    fee: int = 1
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w9xy6edqv9hkptwzewns75ehq53nk8t73je7np5vmj3emps698n9g",
        ),
    ]

    @classmethod
    def order_datum_class(cls) -> type[MinswapStableOrderDatum]:
        """Returns the order datum class used for the DJEDiUSD stable pool."""
        return MinswapStableOrderDatum

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
        fee_on_input: bool = False,
    ) -> tuple[Assets, float]:
        """Calculates the amount out and slippage for given input asset.

        Args:
            asset (Assets): The input asset.
            precise (bool, optional): Whether to calculate precisely. Defaults to True.
            fee_on_input (bool, optional): Whether the fee is applied on the input. Defaults to False

        Returns:
            tuple[Assets, float]: The amount out and slippage.
        """
        out_asset, slippage = super().get_amount_out(
            asset=asset,
            precise=precise,
            fee_on_input=fee_on_input,
        )

        return out_asset, slippage

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
        fee_on_input: bool = False,
    ) -> tuple[Assets, float]:
        """Calculates the amount in and slippage for given output asset.

        Args:
            asset (Assets): The output asset.
            precise (bool, optional): Whether to calculate precisely. Defaults to True.
            fee_on_input (bool, optional): Whether the fee is applied on the input. Defaults to False

        Returns:
            tuple[Assets, float]: The amount in and slippage.
        """
        in_asset, slippage = super().get_amount_in(
            asset=asset,
            precise=precise,
            fee_on_input=fee_on_input,
        )

        return in_asset, slippage

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Performs post-initialization checks and updates.

        Args:
            values (dict[str, Any]): The pool initialization parameters.

        Returns:
            dict[str, Any]: Updated pool initialization parameters.
        """
        super().post_init(values)
        assets = values["assets"]
        datum = MinswapPoolDatum.from_cbor(values["datum_cbor"])

        assets.root[assets.unit()] = datum.balances[0]
        assets.root[assets.unit(1)] = datum.balances[1]

        return values

    @property
    def amp(self) -> int:
        """Returns the amplification factor (amp) of the DJEDiUSD stable pool."""
        return self.pool_datum.amp

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector for the DJEDiUSD stable pool."""
        return PoolSelector(
            selector_type="assets",
            selector=[
                "5d4b6afd3344adcf37ccef5558bb87f522874578c32f17160512e398444a45442d695553442d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(cls) -> type[MinswapDJEDiUSDStablePoolDatum]:
        """Returns the pool datum class used for the DJEDiUSD stable pool."""
        return MinswapDJEDiUSDStablePoolDatum

    @property
    def pool_id(self) -> str:
        """Returns the unique identifier (pool_id) of the DJEDiUSD stable pool."""
        if self.pool_nft is None:
            error_msg = "pool_nft is None"
            raise ValueError(error_msg)
        return self.pool_nft.unit()

    @classmethod
    def pool_policy(cls) -> list[str]:
        """Returns the pool policy for the DJEDiUSD stable pool."""
        return [
            "5d4b6afd3344adcf37ccef5558bb87f522874578c32f17160512e398444a45442d695553442d534c50",
        ]

    @classmethod
    def lp_policy(cls) -> list[str]:
        """Returns the LP policy for the DJEDiUSD stable pool."""
        return []

    @classmethod
    def dex_policy(cls) -> list[str]:
        """Returns the DEX policy for the DJEDiUSD stable pool."""
        return []


class MinswapDJEDUSDCStableState(MinswapDJEDiUSDStableState):
    """Pool Datum for DJEDiUSD stable pool."""

    asset_multippliers: ClassVar[list[int]] = [1, 100]

    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w93d8cuht3hvqt2qqfjqgyek3gk5d6ss2j93e5sh505m0ng8cmze2",
        ),
    ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector for the DJEDUSDC stable pool."""
        return PoolSelector(
            selector_type="assets",
            selector=[
                "d97fa91daaf63559a253970365fb219dc4364c028e5fe0606cdbfff9555344432d444a45442d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(cls) -> type[MinswapDJEDUSDMStablePoolDatum]:
        """Returns the pool datum class used for the DJEDUSDC stable pool."""
        return MinswapDJEDUSDMStablePoolDatum

    @classmethod
    def pool_policy(cls) -> list[str]:
        """Returns the pool policy for the DJEDUSDC stable pool."""
        return [
            "d97fa91daaf63559a253970365fb219dc4364c028e5fe0606cdbfff9555344432d444a45442d534c50",
        ]


class MinswapDJEDUSDMStableState(MinswapDJEDiUSDStableState):
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1wxr9ppdymqgw6g0hvaaa7wc6j0smwh730ujx6lczgdynehsguav8d",
        ),
    ]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=[
                "07b0869ed7488657e24ac9b27b3f0fb4f76757f444197b2a38a15c3c444a45442d5553444d2d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[MinswapDJEDUSDMStablePoolDatum]:
        return MinswapDJEDUSDMStablePoolDatum

    @classmethod
    def pool_policy(cls) -> list[str]:
        return [
            "07b0869ed7488657e24ac9b27b3f0fb4f76757f444197b2a38a15c3c444a45442d5553444d2d534c50",
        ]
