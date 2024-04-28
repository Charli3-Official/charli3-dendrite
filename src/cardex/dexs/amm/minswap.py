from dataclasses import dataclass
from typing import ClassVar
from typing import List
from typing import Union

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.datums import ReceiverDatum
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractCommonStableSwapPoolState
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.utility import Assets
from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script


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
            minimum_receive=in_assets.quantity(),
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
class MinswapOrderDatum(PlutusData):
    """An order datum."""

    CONSTR_ID = 0

    sender: PlutusFullAddress
    receiver: PlutusFullAddress
    receiver_datum_hash: Union[ReceiverDatum | PlutusNone]
    step: Union[SwapExactIn, StableSwapExactIn, SwapExactOut, Deposit, Withdraw, ZapIn]
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
        return self.sender.to_address()

    def requested_amount(self) -> Assets:
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
        if isinstance(self.step, (SwapExactIn, SwapExactOut, StableSwapExactIn)):
            return OrderType.swap
        elif isinstance(self.step, Deposit):
            return OrderType.deposit
        elif isinstance(self.step, Withdraw):
            return OrderType.withdraw
        elif isinstance(self.step, ZapIn):
            return OrderType.zap_in


@dataclass
class MinswapStableOrderDatum(MinswapOrderDatum):
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
    CONSTR_ID = 0
    address: PlutusFullAddress
    fee_to_datum_hash: PlutusNone


@dataclass
class _FeeSwitchWrapper(PlutusData):
    CONSTR_ID = 0
    fee_sharing: FeeSwitchOn


@dataclass
class MinswapPoolDatum(PlutusData):
    """Pool Datum."""

    CONSTR_ID = 0

    asset_a: AssetClass
    asset_b: AssetClass
    total_liquidity: int
    root_k_last: int
    fee_sharing: Union[_FeeSwitchWrapper, PlutusNone]

    def pool_pair(self) -> Assets | None:
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
        raise NotImplementedError


@dataclass
class MinswapDJEDiUSDStablePoolDatum(MinswapStablePoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    def pool_pair(self) -> Assets | None:
        return Assets(
            **{
                "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344": 0,
                "f66d78b4a3cb3d37afa0ec36461e51ecbde00f26c8f0a68f94b6988069555344": 0,
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
                "25c5de5f5b286073c593edfd77b48abc7a48e5a4f3d4cd9d428ff93555534443": 0,
            },
        )


class MinswapCPPState(AbstractConstantProductPoolState):
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


class MinswapDJEDiUSDStableState(AbstractCommonStableSwapPoolState, MinswapCPPState):
    fee: float = 0.5
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
        out_asset, slippage = super().get_amount_out(asset=asset, precise=precise)
        out_asset.root[out_asset.unit()] = (
            out_asset.quantity() * (10000 - self.fee)
        ) / 10000

        if precise:
            out_asset.root[out_asset.unit()] = int(out_asset.quantity())

        return out_asset, slippage

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        asset = Assets(
            **{asset.unit(): (asset.quantity() * (10000 - self.fee)) / 10000},
        )
        in_asset, slippage = super().get_amount_in(asset=asset, precise=precise)
        in_asset.root[in_asset.unit()] = (
            in_asset.quantity() * (10000 - self.fee)
        ) / 10000

        if precise:
            in_asset.root[in_asset.unit()] = int(in_asset.quantity())

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
    def pool_datum_class(self) -> type[MinswapPoolDatum]:
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
    def pool_datum_class(self) -> type[MinswapPoolDatum]:
        return MinswapDJEDUSDMStablePoolDatum

    @classmethod
    @property
    def pool_policy(cls) -> list[str]:
        return [
            "d97fa91daaf63559a253970365fb219dc4364c028e5fe0606cdbfff9555344432d444a45442d534c50",
        ]
