"""Minswap DEX Module."""

from dataclasses import dataclass
from hashlib import sha3_256
from typing import ClassVar
from typing import List
from typing import Union

from pycardano import Address
from pycardano import Datum
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import VerificationKeyHash

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.datums import ReceiverDatum
from charli3_dendrite.dataclasses.datums import _PlutusConstrWrapper
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_types import AbstractCommonStableSwapPoolState
from charli3_dendrite.dexs.amm.amm_types import AbstractConstantProductPoolState
from charli3_dendrite.dexs.amm.sundae import SundaeV3PlutusNone
from charli3_dendrite.dexs.amm.sundae import SundaeV3ReceiverDatumHash
from charli3_dendrite.dexs.amm.sundae import SundaeV3ReceiverInlineDatum
from charli3_dendrite.utility import Assets


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
    coin: int
    expected_receive: int

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

    def order_type(self) -> OrderType | None:
        """The order type."""
        order_type = None
        if isinstance(self.step, (SwapExactIn, SwapExactOut, StableSwapExactIn)):
            order_type = OrderType.swap
        elif isinstance(self.step, (Deposit, StableSwapDeposit, ZapIn)):
            order_type = OrderType.deposit
        elif isinstance(
            self.step,
            (Withdraw, StableSwapWithdraw, StableSwapWithdrawOneCoin),
        ):
            order_type = OrderType.withdraw

        return order_type


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
    deposit_amount_option: Datum
    minimum_lp: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class WithdrawV2(PlutusData):
    """WithdrawV2 order datum."""

    CONSTR_ID = 5
    withdrawal_amount_option: Datum
    minimum_asset_a: int
    minimum_asset_b: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class ZapOutV2(PlutusData):
    """ZapOutV2 order datum."""

    CONSTR_ID = 6
    a_to_b_direction: Union[BoolTrue, BoolFalse]
    withdrawal_amount_option: Datum
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
    withdrawal_amount_optino: Datum
    ratio_asset_a: int
    ratio_asset_b: int
    minimum_asset_a: int
    killable: Union[BoolTrue, BoolFalse]


@dataclass
class SwapMultiRoutingV2(PlutusData):
    """SwapMultiRoutingV2 order datum."""

    CONSTR_ID = 9
    routings: List[Datum]
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
        SundaeV3PlutusNone,
        SundaeV3ReceiverDatumHash,
        SundaeV3ReceiverInlineDatum,
    ]
    receiver_address: PlutusFullAddress
    receiver_datum_hash: Union[
        SundaeV3PlutusNone,
        SundaeV3ReceiverDatumHash,
        SundaeV3ReceiverInlineDatum,
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
                    + pool_name: 0,
                },
            ),
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
                return Assets({"asset_b": self.step.minimum_receive})
            else:
                return Assets({"asset_a": self.step.minimum_receive})
        elif isinstance(self.step, SwapExactOutV2):
            if isinstance(self.step.a_to_b_direction, BoolTrue):
                return Assets({"asset_b": self.step.expected_receive})
            else:
                return Assets({"asset_a": self.step.expected_receive})
        elif isinstance(self.step, DepositV2):
            return Assets({"lp": self.step.minimum_lp})
        elif isinstance(self.step, WithdrawV2):
            return Assets(
                {
                    "asset_a": self.step.minimum_asset_a,
                    "asset_b": self.step.minimum_asset_b,
                },
            )
        else:
            return Assets({})

    def order_type(self) -> OrderType | None:
        """The order type."""
        order_type = None
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
            order_type = OrderType.swap
        elif isinstance(self.step, (DepositV2, DonationV2)):
            order_type = OrderType.deposit
        elif isinstance(self.step, (WithdrawV2, ZapOutV2, WithdrawImbalanceV2)):
            order_type = OrderType.withdraw

        return order_type


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


@dataclass
class MinswapiUSDUSDMStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        return Assets(
            **{
                "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d": 0,
                "f66d78b4a3cb3d37afa0ec36461e51ecbde00f26c8f0a68f94b6988069555344": 0,
            },
        )


@dataclass
class MinswapUSDAUSDMStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        return Assets(
            **{
                "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d": 0,
                "fe7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae45655534441": 0,
            },
        )


@dataclass
class MinswapiUSDUSDCStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        return Assets(
            **{
                "25c5de5f5b286073c593edfd77b48abc7a48e5a4f3d4cd9d428ff93555534443": 0,
                "f66d78b4a3cb3d37afa0ec36461e51ecbde00f26c8f0a68f94b6988069555344": 0,
            },
        )


class MinswapCPPState(AbstractConstantProductPoolState):
    """Minswap Constant Product Pool State."""

    fee: int = 30
    _batcher = Assets(lovelace=900000)
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
        return "Minswap"

    @classmethod
    def order_selector(self) -> list[str]:
        return [s.encode() for s in self._stake_address]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1w8snz7c4974vzdpxu65ruphl3zjdvtxw8strf2c2tmqnxzgusf9xw"],
            assets=[
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
    def order_datum_class(self) -> type[MinswapOrderDatum]:
        return MinswapOrderDatum

    @classmethod
    def script_class(self) -> type[MinswapOrderDatum]:
        return PlutusV1Script

    @classmethod
    def pool_datum_class(self) -> type[MinswapPoolDatum]:
        return MinswapPoolDatum

    # def batcher_fee(
    #     self,
    #     in_assets: Assets | None = None,
    #     out_assets: Assets | None = None,
    #     extra_assets: Assets | None = None,
    # ) -> Assets:
    #     """Batcher fee.

    #     For Minswap, the batcher fee decreases linearly from 2.0 ADA to 1.5 ADA as the
    #     MIN in the input assets from 0 - 50,000 MIN.
    #     """
    #     MIN = "29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c64d494e"
    #     if extra_assets is not None and MIN in extra_assets:
    #         fee_reduction = min(extra_assets[MIN] // 10**5, 500000)
    #     else:
    #         fee_reduction = 0
    #     return self._batcher - Assets(lovelace=fee_reduction)

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def pool_policy(cls) -> list[str]:
        return ["0be55d262b29f564998ff81efe21bdc0022621c12f15af08d0f2ddb1"]

    @classmethod
    def lp_policy(cls) -> list[str]:
        return ["e4214b7cce62ac6fbba385d164df48e157eae5863521b4b67ca71d86"]

    @classmethod
    def dex_policy(cls) -> list[str]:
        return ["13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f"]


class MinswapV2CPPState(AbstractConstantProductPoolState):
    """Minswap Constant Product Pool State."""

    fee: int | list[int] = [30, 30]
    _batcher = Assets(lovelace=700000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w8p79rpkcdz8x9d6tft0x0dx5mwuzac2sa4gm8cvkw5hcnqst2ctf",
        ),
    ]
    _reference_utxo: ClassVar[UTxO | None] = None

    @classmethod
    def dex(cls) -> str:
        return "MinswapV2"

    @classmethod
    def order_selector(self) -> list[str]:
        return [s.encode() for s in self._stake_address]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1w84q0denmyep98ph3tmzwsmw0j7zau9ljmsqx6a4rvaau6ca7j5v4"],
            assets=[
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
    def order_datum_class(self) -> type[MinswapV2OrderDatum]:
        return MinswapV2OrderDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        return PlutusV2Script

    @classmethod
    def script_class(self) -> type[PlutusV2Script]:
        return PlutusV2Script

    @classmethod
    def pool_datum_class(self) -> type[MinswapV2PoolDatum]:
        return MinswapV2PoolDatum

    # def batcher_fee(
    #     self,
    #     in_assets: Assets | None = None,
    #     out_assets: Assets | None = None,
    #     extra_assets: Assets | None = None,
    # ) -> Assets:
    #     """Batcher fee.

    #     For Minswap, the batcher fee decreases linearly from 2.0 ADA to 1.5 ADA as the
    #     MIN in the input assets from 0 - 50,000 MIN.
    #     """
    #     MIN = "29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c64d494e"
    #     if extra_assets is not None and MIN in extra_assets:
    #         fee_reduction = min(extra_assets[MIN] // 10**5, 500000)
    #     else:
    #         fee_reduction = 0
    #     return self._batcher - Assets(lovelace=fee_reduction)

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.lp_tokens.unit()

    @classmethod
    def lp_policy(cls) -> list[str]:
        return ["f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c"]

    @classmethod
    def dex_policy(cls) -> list[str]:
        return ["f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c4d5350"]

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        datum = MinswapV2PoolDatum.from_cbor(values["datum_cbor"])

        assets = values["assets"]
        assets.root[assets.unit()] = datum.reserve_a
        assets.root[assets.unit(1)] = datum.reserve_b

        values["fee"] = [datum.base_fee_a_numerator, datum.base_fee_b_numerator]

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Retrieve the reference UTxO for the Spectrum DEX.

        This method checks if the reference UTxO is already set. If not, it retrieves
        the script bytes from the stake address and sets the reference UTxO.

        Returns:
            UTxO | None: The reference UTxO if available, otherwise None.
        """
        if cls._reference_utxo is None:
            cls._reference_utxo = UTxO(
                input=TransactionInput(
                    transaction_id=TransactionId(
                        bytes.fromhex(
                            "cf4ecddde0d81f9ce8fcc881a85eb1f8ccdaf6807f03fea4cd02da896a621776",
                        ),
                    ),
                    index=0,
                ),
                output=TransactionOutput(
                    address=Address.decode(
                        "addr1qyqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqzj2c79gy9l76sdg0xwhd7r0c0kna0tycz4y5s6mlenh8pq6a0h00",
                    ),
                    amount=Value(coin=12486070),
                    script=PlutusV2Script(
                        bytes.fromhex(
                            "590a600100003332323232323232323222222533300832323232533300c3370e900118058008991919299980799b87480000084cc004dd5980a180a980a980a980a980a980a98068030060a99980799b87480080084c8c8c8c8c8c8c8c8c8c8c8c8c8c8c8c8c94ccc080cdc3a4000002264646600200200e44a66604c00229404c8c94ccc094cdc78010028a51133004004001302a002375c60500026eb8c094c07800854ccc080cdc3a40040022646464646600200202844a66605000229404c8c94ccc09ccdd798161812981618129816181698128010028a51133004004001302c002302a0013374a9001198131ba90014bd701bae3026001301e002153330203370e900200089980900419ba548000cc090cdd2a400466048604a603c00497ae04bd70099981019b87375a6044604a66446464a66604866e1d200200114bd6f7b63009bab302930220023022001323300100100322533302700114c103d87a800013232323253330283371e00e004266e9520003302c374c00297ae0133006006003375660520066eb8c09c008c0ac008c0a4004c8cc004004030894ccc09400452f5bded8c0264646464a66604c66e3d22100002100313302a337606ea4008dd3000998030030019bab3027003375c604a0046052004604e0026eb8c094c07800920004a0944c078004c08c004c06c060c8c8c8c8c8c8c94ccc08ccdc3a40000022646464646464646464646464646464646464a6660706076004264646464646464649319299981e99b87480000044c8c94ccc108c1140084c92632375a60840046eb4c10000458c8cdd81822000982218228009bac3043001303b0091533303d3370e90010008a999820181d8048a4c2c2c607601064a66607866e1d2000001132323232323232325333047304a002132498c09401458cdc3a400460886ea8c120004c120008dd6982300098230011822000982200119b8748008c0f8dd51821000981d0060a99981e19b87480080044c8c8c8c8c8c94ccc114c1200084c926302300316375a608c002608c0046088002608800466e1d2002303e3754608400260740182a66607866e1d2004001132323232323232325333047304a002132498c09401458dd6982400098240011bad30460013046002304400130440023370e9001181f1baa3042001303a00c1533303c3370e9003000899191919191919192999823982500109924c604a00a2c66e1d200230443754609000260900046eb4c118004c118008c110004c110008cdc3a4004607c6ea8c108004c0e803054ccc0f0cdc3a40100022646464646464a66608a60900042649319299982199b87480000044c8c8c8c94ccc128c13400852616375a609600260960046eb4c124004c10401854ccc10ccdc3a4004002264646464a666094609a0042930b1bad304b001304b002375a6092002608200c2c608200a2c66e1d200230423754608c002608c0046eb4c110004c110008c108004c0e803054ccc0f0cdc3a401400226464646464646464a66608e60940042649318130038b19b8748008c110dd5182400098240011bad30460013046002375a60880026088004608400260740182a66607866e1d200c001132323232323232325333047304a002132498c09801458cdc3a400460886ea8c120004c120008dd6982300098230011822000982200119b8748008c0f8dd51821000981d0060a99981e19b87480380044c8c8c8c8c8c8c8c8c8c8c8c8c8c94ccc134c14000852616375a609c002609c0046eb4c130004c130008dd6982500098250011bad30480013048002375a608c002608c0046eb4c110004c110008cdc3a4004607c6ea8c108004c0e803054ccc0f0cdc3a4020002264646464646464646464a66609260980042649318140048b19b8748008c118dd5182500098250011bad30480013048002375a608c002608c0046eb4c110004c110008c108004c0e803054ccc0f0cdc3a40240022646464646464a66608a60900042646493181200219198008008031129998238008a4c2646600600660960046464a66608c66e1d2000001132323232533304d3050002132498c0b400c58cdc3a400460946ea8c138004c138008c130004c11000858c110004c12400458dd698230009823001182200098220011bac3042001303a00c1533303c3370e900a0008a99981f981d0060a4c2c2c6074016603a018603001a603001c602c01e602c02064a66606c66e1d200000113232533303b303e002149858dd7181e000981a0090a99981b19b87480080044c8c94ccc0ecc0f800852616375c607800260680242a66606c66e1d200400113232533303b303e002149858dd7181e000981a0090a99981b19b87480180044c8c94ccc0ecc0f800852616375c607800260680242c60680222c607200260720046eb4c0dc004c0dc008c0d4004c0d4008c0cc004c0cc008c0c4004c0c4008c0bc004c0bc008c0b4004c0b4008c0ac004c0ac008c0a4004c08407858c0840748c94ccc08ccdc3a40000022a66604c60420042930b0a99981199b87480080044c8c94ccc0a0c0ac00852616375c605200260420042a66604666e1d2004001132325333028302b002149858dd7181480098108010b1810800919299981119b87480000044c8c8c8c94ccc0a4c0b00084c8c9263253330283370e9000000899192999816981800109924c64a66605666e1d20000011323253330303033002132498c04400458c0c4004c0a400854ccc0accdc3a40040022646464646464a666068606e0042930b1bad30350013035002375a606600260660046eb4c0c4004c0a400858c0a400458c0b8004c09800c54ccc0a0cdc3a40040022a666056604c0062930b0b181300118050018b18150009815001181400098100010b1810000919299981099b87480000044c8c94ccc098c0a400852616375a604e002603e0042a66604266e1d20020011323253330263029002149858dd69813800980f8010b180f800919299981019b87480000044c8c94ccc094c0a000852616375a604c002603c0042a66604066e1d20020011323253330253028002149858dd69813000980f0010b180f000919299980f99b87480000044c8c8c8c94ccc098c0a400852616375c604e002604e0046eb8c094004c07400858c0740048c94ccc078cdc3a400000226464a666046604c0042930b1bae3024001301c0021533301e3370e900100089919299981198130010a4c2c6eb8c090004c07000858c070004dd618100009810000980f8011bab301d001301d001301c00237566034002603400260320026030002602e0046eb0c054004c0340184cc004dd5980a180a980a980a980a980a980a980680300591191980080080191299980a8008a50132323253330153375e00c00229444cc014014008c054008c064008c05c004c03001cc94ccc034cdc3a40000022a666020601600e2930b0a99980699b874800800454ccc040c02c01c526161533300d3370e90020008a99980818058038a4c2c2c601600c2c60200026020004601c002600c00229309b2b118029baa001230033754002ae6955ceaab9e5573eae815d0aba24c126d8799fd87a9f581c1eae96baf29e27682ea3f815aba361a0c6059d45e4bfbe95bbd2f44affff004c0126d8799fd87a9f581cc8b0cc61374d409ff9c8512317003e7196a3e4d48553398c656cc124ffff0001",
                        ),
                    ),
                ),
            )

        return cls._reference_utxo


class MinswapDJEDiUSDStableState(AbstractCommonStableSwapPoolState, MinswapCPPState):
    """Minswap DJED/iUSD Stable State."""

    fee: float = 1
    _batcher = Assets(lovelace=600000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w9xy6edqv9hkptwzewns75ehq53nk8t73je7np5vmj3emps698n9g",
        ),
    ]

    @classmethod
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

        datum = cls.pool_datum_class().from_cbor(values["datum_cbor"])

        assets.root[assets.unit()] = datum.balances[0]
        assets.root[assets.unit(1)] = datum.balances[1]

        return values

    @property
    def amp(self) -> int:
        return self.pool_datum.amp

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1wy7kkcpuf39tusnnyga5t2zcul65dwx9yqzg7sep3cjscesx2q5m5"],
            assets=[
                "5d4b6afd3344adcf37ccef5558bb87f522874578c32f17160512e398444a45442d695553442d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[MinswapDJEDiUSDStablePoolDatum]:
        return MinswapDJEDiUSDStablePoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def pool_policy(cls) -> list[str]:
        return [
            "5d4b6afd3344adcf37ccef5558bb87f522874578c32f17160512e398444a45442d695553442d534c50",
        ]

    @classmethod
    def lp_policy(cls) -> list[str] | None:
        return None

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        return None


class MinswapDJEDUSDCStableState(MinswapDJEDiUSDStableState):
    """Minswap DJED/USDC Stable State."""

    asset_mulitipliers: ClassVar[list[int]] = [1, 100]

    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w93d8cuht3hvqt2qqfjqgyek3gk5d6ss2j93e5sh505m0ng8cmze2",
        ),
    ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1wx8d45xlfrlxd7tctve8xgdtk59j849n00zz2pgyvv47t8sxa6t53"],
            assets=[
                "d97fa91daaf63559a253970365fb219dc4364c028e5fe0606cdbfff9555344432d444a45442d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[MinswapDJEDUSDCStablePoolDatum]:
        return MinswapDJEDUSDCStablePoolDatum

    @classmethod
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
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1wxxdvtj6y4fut4tmu796qpvy2xujtd836yg69ahat3e6jjcelrf94"],
            assets=[
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


class MinswapiUSDUSDMStableState(MinswapDJEDiUSDStableState):
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1wxtv9k2lcum5pmcc4wu44a5tufulszahz84knff87wcawycez9lug",
        ),
    ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1w9520fyp6g3pjwd0ymfy4v2xka54ek6ulv4h8vce54zfyfcm2m0sm"],
            assets=[
                "96402c6f5e7a04f16b4d6f500ab039ff5eac5d0226d4f88bf5523ce85553444d2d695553442d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[MinswapiUSDUSDMStablePoolDatum]:
        return MinswapiUSDUSDMStablePoolDatum

    @classmethod
    def pool_policy(cls) -> list[str]:
        return [
            "96402c6f5e7a04f16b4d6f500ab039ff5eac5d0226d4f88bf5523ce85553444d2d695553442d534c50",
        ]


class MinswapUSDAUSDMStableState(MinswapDJEDiUSDStableState):
    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1w8cafpjmeer4j8t8aseqayhwkf4ezuufue0clvfthxecsacv83rt0",
        ),
    ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1wywdvw0qwv2n97e8y5jsfqq3qryu6re3gxwqcc7fzscpwugxz5dwe"],
            assets=[
                "a0d806e67be578911ca39260cff5eaa6eb06f9f4165ccd570282f5055553444d2d555344412d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[MinswapUSDAUSDMStablePoolDatum]:
        return MinswapUSDAUSDMStablePoolDatum

    @classmethod
    def pool_policy(cls) -> list[str]:
        return [
            "a0d806e67be578911ca39260cff5eaa6eb06f9f4165ccd570282f5055553444d2d555344412d534c50",
        ]


class MinswapiUSDUSDCStableState(MinswapDJEDiUSDStableState):
    asset_mulitipliers: ClassVar[list[int]] = [1, 100]

    _stake_address: ClassVar[Address] = [
        Address.from_primitive(
            "addr1wy42rt3rdptdaa2lwlntkx49ksuqrmqqjlu7pf5l5f8upmgj3gq2m",
        ),
    ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1wx4w03kq5tfhaad2fmglefgejj0anajcsvvg88w96lrmylc7mx5rm"],
            assets=[
                "739150a2612da82e16adc2a3a1f88b256202d8415df0c5b7a2ff93fb555344432d695553442d302e312d534c50",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[MinswapiUSDUSDCStablePoolDatum]:
        return MinswapiUSDUSDCStablePoolDatum

    @classmethod
    def pool_policy(cls) -> list[str]:
        return [
            "739150a2612da82e16adc2a3a1f88b256202d8415df0c5b7a2ff93fb555344432d695553442d302e312d534c50",
        ]
