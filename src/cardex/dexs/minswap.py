from dataclasses import dataclass
from typing import ClassVar
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.datums import ReceiverDatum
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm_types import AbstractConstantProductPoolState
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
    step: Union[SwapExactIn, SwapExactOut, Deposit, Withdraw, ZapIn]
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
        if isinstance(self.step, (SwapExactIn, SwapExactOut)):
            return OrderType.swap
        elif isinstance(self.step, Deposit):
            return OrderType.deposit
        elif isinstance(self.step, Withdraw):
            return OrderType.withdraw
        elif isinstance(self.step, ZapIn):
            return OrderType.zap_in


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
