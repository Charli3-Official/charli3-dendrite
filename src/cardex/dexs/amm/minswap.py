"""Minswap AMM module."""

from dataclasses import dataclass
from hashlib import sha3_256
from typing import Any
from typing import ClassVar
from typing import List
from typing import Union

from pycardano import Address
from pycardano import VerificationKeyHash
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script

from cardex.dataclasses.datums import _PlutusConstrWrapper
from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.datums import ReceiverDatum
from cardex.dataclasses.datums import PoolDatum
from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractCommonStableSwapPoolState
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.amm.sundae import SundaeV3PlutusNone
from cardex.dexs.amm.sundae import SundaeV3ReceiverDatumHash
from cardex.dexs.amm.sundae import SundaeV3ReceiverInlineDatum
from cardex.utility import Assets


@dataclass
class SwapExactIn(PlutusData):
    """Swap exact in order datum."""

    CONSTR_ID = 0
    desired_coin: AssetClass
    minimum_receive: int

    @classmethod
    def from_assets(cls, asset: Assets):
        """Parse an Assets object into a SwapExactIn datum."""
        assert len(asset) == 1

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
    def from_assets(cls, in_assets: Assets, out_assets: Assets):
        """Parse an Assets object into a SwapExactIn datum."""
        assert len(in_assets) == 1
        assert len(out_assets) == 1

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
    def from_assets(cls, asset: Assets):
        """Parse an Assets object into a SwapExactOut datum."""
        assert len(asset) == 1

        return StableSwapDeposit(
            expected_receive=asset.quantity(),
        )


@dataclass
class StableSwapWithdraw(PlutusData):
    """Swap exact out order datum."""

    CONSTR_ID = 2
    expected_receive: List[int]

    @classmethod
    def from_assets(cls, asset: Assets):
        """Parse an Assets object into a SwapExactOut datum."""
        assert len(asset) == 2

        return StableSwapWithdraw(
            expected_receive=[asset.quantity(), asset.quantity(1)],
        )


@dataclass
class StableSwapWithdrawOneCoin(PlutusData):
    """Swap exact out order datum."""

    CONSTR_ID = 4
    expected_receive: Any

    @classmethod
    def from_assets(cls, coin_index: int, asset: Assets):
        """Parse an Assets object into a SwapExactOut datum."""
        assert len(asset) == 1

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
    def from_assets(cls, asset: Assets):
        """Parse an Assets object into a SwapExactOut datum."""
        assert len(asset) == 1

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
    receiver_datum_hash: Union[ReceiverDatum, PlutusNone]
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
    def create_datum(
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,
        deposit: Assets,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ):
        """Create an order datum."""
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

    def address_source(self) -> Address:
        """The source address."""
        return self.sender.to_address()

    def requested_amount(self) -> Assets:
        """The requested amount."""
        if isinstance(self.step, SwapExactIn):
            return Assets(
                {self.step.desired_coin.assets.unit(): self.step.minimum_receive},
            )
        elif isinstance(self.step, SwapExactOut):
            return Assets(
                {self.step.desired_coin.assets.unit(): self.step.expected_receive},
            )
        elif isinstance(self.step, Deposit):
            return Assets({"lp": self.step.minimum_lp})
        elif isinstance(self.step, Withdraw):
            return Assets(
                {"asset_a": self.step.min_asset_a, "asset_b": self.step.min_asset_a},
            )
        elif isinstance(self.step, ZapIn):
            return Assets({self.step.desired_coin.assets.unit(): self.step.minimum_lp})

    def order_type(self) -> OrderType:
        """The order type."""
        if isinstance(self.step, (SwapExactIn, SwapExactOut, StableSwapExactIn)):
            return OrderType.swap
        elif isinstance(self.step, (Deposit, StableSwapDeposit)):
            return OrderType.deposit
        elif isinstance(
            self.step,
            (Withdraw, StableSwapWithdraw, StableSwapWithdrawOneCoin),
        ):
            return OrderType.withdraw
        elif isinstance(self.step, ZapIn):
            return OrderType.zap_in


@dataclass
class BoolFalse(PlutusData):
    CONSTR_ID = 0


@dataclass
class BoolTrue(PlutusData):
    CONSTR_ID = 1


@dataclass
class SAOSpecificAmount(PlutusData):
    CONSTR_ID = 0

    swap_amount: int


@dataclass
class SAOAll(PlutusData):
    CONSTR_ID = 1

    deducted_amount: int


@dataclass
class SwapAmountOption(PlutusData):
    CONSTR_ID = 0

    option: Union[SAOSpecificAmount, SAOAll]


@dataclass
class SwapExactInV2(PlutusData):
    """Swap exact in order datum."""

    CONSTR_ID = 0
    a_to_b_direction: Union[BoolTrue, BoolFalse]
    swap_amount_option: Union[SAOSpecificAmount, SAOAll]
    minimum_receive: int
    killable: Union[BoolTrue, BoolFalse]

    @classmethod
    def from_assets(cls, in_asset: Assets, out_asset: Assets) -> "SwapExactInV2":
        """Parse an Assets object into a SwapExactInV2 datum."""
        assert len(in_asset) == 1

        merged_assets = in_asset + out_asset

        direction = (
            BoolTrue() if in_asset.unit() == merged_assets.unit() else BoolFalse()
        )

        option = SAOSpecificAmount(swap_amount=in_asset.quantity())

        return cls(
            a_to_b_direction=direction,
            swap_amount_option=option,
            minimum_receive=out_asset.quantity(),
            killable=BoolFalse(),
        )


@dataclass
class StopLossV2(PlutusData):
    """Stop loss order datum."""

    CONSTR_ID = 1
    a_to_b_direction: Union[BoolTrue, BoolFalse]
    swap_amount_option: Union[SAOSpecificAmount, SAOAll]
    stop_loss_receive: int


@dataclass
class OCOV2(PlutusData):
    """OCO order datum."""

    CONSTR_ID = 2
    a_to_b_direction: Union[BoolTrue, BoolFalse]
    swap_amount_option: Union[SAOSpecificAmount, SAOAll]
    minimum_receive: int
    stop_loss_receive: int


@dataclass
class SwapExactOutV2(PlutusData):
    """Swap exact out order datum."""

    CONSTR_ID = 3
    a_to_b_direction: Union[BoolTrue, BoolFalse]
    swap_amount_option: Union[SAOSpecificAmount, SAOAll]
    expected_receive: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class DepositV2(PlutusData):
    """DepositV2 order datum."""

    CONSTR_ID = 4
    deposit_amount_option: Any
    minimum_lp: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class WithdrawV2(PlutusData):
    """WithdrawV2 order datum."""

    CONSTR_ID = 5
    withdrawal_amount_option: Any
    minimum_asset_a: int
    minimum_asset_b: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class ZapOutV2(PlutusData):
    """ZapOutV2 order datum."""

    CONSTR_ID = 6
    a_to_b_direction: Union[BoolTrue, BoolFalse]
    withdrawal_amount_option: Any
    minimum_receive: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class PartialSwapV2(PlutusData):
    """PartialSwapV2 order datum."""

    CONSTR_ID = 7
    a_to_b_direction: Union[BoolTrue, BoolFalse]
    total_swap_amount: int
    io_ratio_numerator: int
    io_ratio_denominator: int
    hops: int
    minimum_swap_amount_required: int
    max_batcher_fee_each_time: int


@dataclass
class WithdrawImbalanceV2(PlutusData):
    """WithdrawImbalanceV2 order datum."""

    CONSTR_ID = 8
    withdrawal_amount_optino: Any
    ratio_asset_a: int
    ratio_asset_b: int
    minimum_asset_a: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class SwapMultiRoutingV2(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 9
    routings: List[Any]
    swap_amount_option: Union[SAOSpecificAmount, SAOAll]
    minimum_receive: int


@dataclass
class DonationV2(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 10


@dataclass
class OAMSignature(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 0

    pub_key_hash: bytes


@dataclass
class OAMSpend(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 1

    script_hash: bytes


@dataclass
class OAMWithdraw(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 2

    script_hash: bytes


@dataclass
class OAMMint(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 3

    script_hash: bytes


@dataclass
class Expire(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 0

    ttl: List[int]


@dataclass
class MinswapV2OrderDatum(OrderDatum):
    """An order datum."""

    owner: Union[OAMMint, OAMSignature, OAMSpend, OAMWithdraw]
    refund_address: PlutusFullAddress
    refund_datum_hash: Union[
        SundaeV3PlutusNone, SundaeV3ReceiverDatumHash, SundaeV3ReceiverInlineDatum
    ]
    receiver_address: PlutusFullAddress
    receiver_datum_hash: Union[
        SundaeV3PlutusNone, SundaeV3ReceiverDatumHash, SundaeV3ReceiverInlineDatum
    ]
    lp_asset: AssetClass
    step: Union[
        SwapExactInV2,
        StopLossV2,
        OCOV2,
        SwapExactOutV2,
        DepositV2,
        WithdrawV2,
        ZapOutV2,
        PartialSwapV2,
        WithdrawImbalanceV2,
        SwapMultiRoutingV2,
        DonationV2,
    ]
    max_batcher_fee: int
    expiration_setting: Union[PlutusNone, Expire]

    @classmethod
    def create_datum(
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,
        deposit: Assets,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ):
        """Create an order datum."""
        full_address_source = PlutusFullAddress.from_address(address_source)
        step = SwapExactInV2.from_assets(in_asset=in_assets, out_asset=out_assets)

        if address_target is None:
            address_target = address_source
            datum_target = SundaeV3PlutusNone()
        elif datum_target is None:
            datum_target = SundaeV3PlutusNone()

        full_address_target = PlutusFullAddress.from_address(address_target)

        merged_assets = in_assets + out_assets

        if merged_assets.unit() == "lovelace":
            token_a = sha3_256(bytes.fromhex("")).digest()
        else:
            token_a = sha3_256(bytes.fromhex(merged_assets.unit())).digest()
        token_b = sha3_256(bytes.fromhex(merged_assets.unit(1))).digest()
        pool_name = sha3_256(token_a + token_b).hexdigest()
        lp_asset = AssetClass.from_assets(
            Assets(
                **{
                    "f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c"
                    + pool_name: 0
                }
            )
        )

        return cls(
            owner=OAMSignature(address_source.payment_part.payload),
            refund_address=full_address_source,
            refund_datum_hash=datum_target,
            receiver_address=full_address_target,
            receiver_datum_hash=datum_target,
            lp_asset=lp_asset,
            step=step,
            max_batcher_fee=batcher_fee.quantity(),
            expiration_setting=PlutusNone(),
        )

    def address_source(self) -> Address:
        """The source address."""
        if isinstance(self.owner, OAMSignature):
            h = self.owner.pub_key_hash
        else:
            h = self.owner.script_hash

        return Address(payment_part=VerificationKeyHash(h))

    def requested_amount(self) -> Assets:
        """The requested amount."""
        if isinstance(self.step, SwapExactInV2):
            if isinstance(self.step.a_to_b_direction, BoolTrue):
                return Assets({"asset_a": self.step.minimum_receive})
            else:
                return Assets({"asset_b": self.step.minimum_receive})
        elif isinstance(self.step, SwapExactOutV2):
            if isinstance(self.step.a_to_b_direction, BoolTrue):
                return Assets({"asset_a": self.step.expected_receive})
            else:
                return Assets({"asset_b": self.step.expected_receive})
        elif isinstance(self.step, DepositV2):
            return Assets({"lp": self.step.expected_receive})
        elif isinstance(self.step, WithdrawV2):
            return Assets(
                {
                    "asset_a": self.step.minimum_asset_a,
                    "asset_b": self.step.minimum_asset_b,
                },
            )
        else:
            return Assets({})

    def order_type(self) -> OrderType:
        """The order type."""
        if isinstance(
            self.step,
            (
                SwapExactInV2,
                StopLossV2,
                OCOV2,
                SwapExactOutV2,
                PartialSwapV2,
                SwapMultiRoutingV2,
            ),
        ):
            return OrderType.swap
        elif isinstance(self.step, DepositV2, DonationV2):
            return OrderType.deposit
        elif isinstance(self.step, (WithdrawV2, ZapOutV2, WithdrawImbalanceV2)):
            return OrderType.withdraw


@dataclass
class MinswapStableOrderDatum(MinswapOrderDatum):
    """MinSwap Stable Order Datum."""

    @classmethod
    def create_datum(
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,
        deposit: Assets,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ):
        """Create an order datum."""
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
        """Return the asset pair associated with the pool."""
        return self.asset_a.assets + self.asset_b.assets


@dataclass
class OptionalInt(PlutusData):
    CONSTR_ID = 0

    value: int


@dataclass
class MinswapV2PoolDatum(PoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    pool_batching_stake_credential: _PlutusConstrWrapper
    asset_a: AssetClass
    asset_b: AssetClass
    total_liquidity: int
    reserve_a: int
    reserve_b: int
    base_fee_a_numerator: int
    base_fee_b_numerator: int
    fee_sharing_numerator: Union[PlutusNone, OptionalInt]
    allow_dynamic_fee: Union[BoolTrue, BoolFalse]

    def pool_pair(self) -> Assets | None:
        """Return the asset pair associated with the pool."""
        return self.asset_a.assets + self.asset_b.assets


@dataclass
class MinswapStablePoolDatum(PlutusData):
    """Stable Pool Datum."""

    CONSTR_ID = 0

    balances: List[int]
    total_liquidity: int
    amp: int
    order_hash: bytes

    def pool_pair(self) -> Assets | None:
        """Return the asset pair associated with the pool."""
        raise NotImplementedError


@dataclass
class MinswapDJEDiUSDStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        """Return the asset pair associated with the pool."""
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
        return Assets(
            **{
                "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344": 0,
                "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d": 0,
            },
        )


class MinswapCPPState(AbstractConstantProductPoolState):
    """Minswap Constant Product Pool State."""

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
    @property
    def dex(cls) -> str:
        return "Minswap"

    @classmethod
    @property
    def order_selector(self) -> list[str]:
        return [s.encode() for s in self._stake_address]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=[
                "13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f4d494e53574150",
            ],
        )

    @property
    def swap_forward(self) -> bool:
        return True

    @property
    def stake_address(self) -> Address:
        return self._stake_address[0]

    @classmethod
    @property
    def order_datum_class(self) -> type[MinswapOrderDatum]:
        return MinswapOrderDatum

    @classmethod
    @property
    def script_class(self) -> type[MinswapOrderDatum]:
        return PlutusV1Script

    @classmethod
    @property
    def pool_datum_class(self) -> type[MinswapPoolDatum]:
        return MinswapPoolDatum

    def batcher_fee(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
        extra_assets: Assets | None = None,
    ) -> Assets:
        """Batcher fee.

        For Minswap, the batcher fee decreases linearly from 2.0 ADA to 1.5 ADA as the
        MIN in the input assets from 0 - 50,000 MIN.
        """
        MIN = "29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c64d494e"
        if extra_assets is not None and MIN in extra_assets:
            fee_reduction = min(extra_assets[MIN] // 10**5, 500000)
        else:
            fee_reduction = 0
        return self._batcher - Assets(lovelace=fee_reduction)

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    @property
    def pool_policy(cls) -> list[str]:
        return ["0be55d262b29f564998ff81efe21bdc0022621c12f15af08d0f2ddb1"]

    @classmethod
    @property
    def lp_policy(cls) -> list[str]:
        return ["e4214b7cce62ac6fbba385d164df48e157eae5863521b4b67ca71d86"]

    @classmethod
    @property
    def dex_policy(cls) -> list[str]:
        return ["13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f"]


class MinswapV2CPPState(AbstractConstantProductPoolState):
    """Minswap Constant Product Pool State."""

    fee: int | list[int] = [30, 30]
    _batcher = Assets(lovelace=1000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1z8p79rpkcdz8x9d6tft0x0dx5mwuzac2sa4gm8cvkw5hcn864negmna25tfcqjjxj65tnk0d0fmkza3gjdrxweaff35q0ym7k8",
        ),
    ]

    @classmethod
    @property
    def dex(cls) -> str:
        return "MinswapV2"

    @classmethod
    @property
    def order_selector(self) -> list[str]:
        return [s.encode() for s in self._stake_address]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=[
                "f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c4d5350",
            ],
        )

    @property
    def swap_forward(self) -> bool:
        return True

    @property
    def stake_address(self) -> Address:
        return self._stake_address[0]

    @classmethod
    @property
    def order_datum_class(self) -> type[MinswapV2OrderDatum]:
        return MinswapV2OrderDatum

    @classmethod
    @property
    def script_class(self) -> type[PlutusV2Script]:
        return PlutusV2Script

    @classmethod
    @property
    def pool_datum_class(self) -> type[MinswapV2PoolDatum]:
        return MinswapV2PoolDatum

    def batcher_fee(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
        extra_assets: Assets | None = None,
    ) -> Assets:
        """Batcher fee.

        For Minswap, the batcher fee decreases linearly from 2.0 ADA to 1.5 ADA as the
        MIN in the input assets from 0 - 25,000 MIN.
        """
        MIN = "29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c64d494e"
        if extra_assets is not None and MIN in extra_assets:
            fee_reduction = min(extra_assets[MIN] // (2 * 10**5), 250000)
        else:
            fee_reduction = 0
        return self._batcher - Assets(lovelace=fee_reduction)

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.lp_tokens.unit()

    # @classmethod
    # @property
    # def pool_policy(cls) -> list[str]:
    #     return ["0be55d262b29f564998ff81efe21bdc0022621c12f15af08d0f2ddb1"]

    @classmethod
    @property
    def lp_policy(cls) -> list[str]:
        return ["f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c"]

    @classmethod
    @property
    def dex_policy(cls) -> list[str]:
        return ["f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c"]

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        datum = MinswapV2PoolDatum.from_cbor(values["datum_cbor"])

        assets = values["assets"]
        assets.root[assets.unit()] = datum.reserve_a
        assets.root[assets.unit(1)] = datum.reserve_b

        values["fee"] = [datum.base_fee_a_numerator, datum.base_fee_b_numerator]


class MinswapDJEDiUSDStableState(AbstractCommonStableSwapPoolState, MinswapCPPState):
    """Minswap DJED/iUSD Stable State."""

    fee: float = 1
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w9xy6edqv9hkptwzewns75ehq53nk8t73je7np5vmj3emps698n9g",
        ),
    ]

    @classmethod
    @property
    def order_datum_class(cls) -> type[MinswapStableOrderDatum]:
        return MinswapStableOrderDatum

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        out_asset, slippage = super().get_amount_out(
            asset=asset,
            precise=precise,
            fee_on_input=False,
        )

        return out_asset, slippage

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        in_asset, slippage = super().get_amount_in(
            asset=asset,
            precise=precise,
            fee_on_input=False,
        )

        return in_asset, slippage

    @classmethod
    def post_init(cls, values: dict[str, ...]):
        """Post initialization checks.

        Args:
            values: The pool initialization parameters
        """
        super().post_init(values)
        assets = values["assets"]

        datum = cls.pool_datum_class.from_cbor(values["datum_cbor"])

        assets.root[assets.unit()] = datum.balances[0]
        assets.root[assets.unit(1)] = datum.balances[1]

        return values

    @property
    def amp(self) -> int:
        return self.pool_datum.amp

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=[
                "5d4b6afd3344adcf37ccef5558bb87f522874578c32f17160512e398444a45442d695553442d534c50",
            ],
        )

    @classmethod
    @property
    def pool_datum_class(self) -> type[MinswapDJEDiUSDStablePoolDatum]:
        return MinswapDJEDiUSDStablePoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    @property
    def pool_policy(cls) -> list[str]:
        return [
            "5d4b6afd3344adcf37ccef5558bb87f522874578c32f17160512e398444a45442d695553442d534c50",
        ]

    @classmethod
    @property
    def lp_policy(cls) -> list[str] | None:
        return None

    @classmethod
    @property
    def dex_policy(cls) -> list[str] | None:
        return None


class MinswapDJEDUSDCStableState(MinswapDJEDiUSDStableState):
    """Minswap DJED/USDC Stable State."""

    asset_mulitipliers: list[int] = [1, 100]

    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w93d8cuht3hvqt2qqfjqgyek3gk5d6ss2j93e5sh505m0ng8cmze2",
        ),
    ]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=[
                "d97fa91daaf63559a253970365fb219dc4364c028e5fe0606cdbfff9555344432d444a45442d534c50",
            ],
        )

    @classmethod
    @property
    def pool_datum_class(self) -> type[MinswapDJEDUSDCStablePoolDatum]:
        return MinswapDJEDUSDCStablePoolDatum

    @classmethod
    @property
    def pool_policy(cls) -> list[str]:
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
    @property
    def pool_datum_class(self) -> type[MinswapDJEDUSDMStablePoolDatum]:
        return MinswapDJEDUSDMStablePoolDatum

    @classmethod
    @property
    def pool_policy(cls) -> list[str]:
        return [
            "07b0869ed7488657e24ac9b27b3f0fb4f76757f444197b2a38a15c3c444a45442d5553444d2d534c50",
        ]
