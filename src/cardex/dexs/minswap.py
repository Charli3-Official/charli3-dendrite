from dataclasses import dataclass

from pycardano import Address
from pycardano import DatumHash
from pycardano import PlutusData
from pycardano import TransactionOutput

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.abstract_classes import AbstractConstantProductPoolState
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
class ReceiverDatum(PlutusData):
    """The receiver address."""

    CONSTR_ID = 1
    datum_hash: DatumHash | None


@dataclass
class MinswapOrderDatum(PlutusData):
    """An order datum."""

    CONSTR_ID = 0

    sender: PlutusFullAddress
    receiver: PlutusFullAddress
    receiver_datum_hash: DatumHash | PlutusNone
    step: SwapExactIn | SwapExactOut
    batcher_fee: int
    deposit: int


@dataclass
class FeeDatumHash(PlutusData):
    """Fee datum hash."""

    CONSTR_ID = 0
    fee_hash: str


@dataclass
class FeeSwitchOn(PlutusData):
    """Pool Fee Sharing On."""

    CONSTR_ID = 0
    fee_to: PlutusFullAddress
    fee_to_datum_hash: PlutusNone | FeeDatumHash


@dataclass
class _EmptyFeeSwitchWrapper(PlutusData):
    CONSTR_ID = 0
    fee_sharing: FeeSwitchOn | PlutusNone


@dataclass
class MinswapPoolDatum(PlutusData):
    """Pool Datum."""

    CONSTR_ID = 0

    asset_a: AssetClass
    asset_b: AssetClass
    total_liquidity: int
    root_k_last: int
    fee_sharing: _EmptyFeeSwitchWrapper


@dataclass
class CancelRedeemer(PlutusData):
    """Cancel datum."""

    CONSTR_ID = 1


class MinswapCPPState(AbstractConstantProductPoolState):
    fee: int = 30
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address = Address.from_primitive(
        "addr1zxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uw6j2c79gy9l76sdg0xwhd7r0c0kna0tycz4y5s6mlenh8pq6s3z70",
    )

    @classmethod
    @property
    def dex(cls) -> str:
        return "Minswap"

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=[
                "13aa2accf2e1561723aa26871e071fdf32c867cff7e7d50ad470d62f4d494e53574150",
            ],
        )

    @classmethod
    @property
    def order_datum_class(self) -> type[MinswapOrderDatum]:
        return MinswapOrderDatum

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

    def swap_tx_output(
        self,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        slippage: float = 0.005,
    ) -> tuple[TransactionOutput, MinswapOrderDatum]:
        # Basic checks
        if 1 in [len(in_assets), len(out_assets)]:
            raise ValueError(
                "Only one asset can be supplied as input, "
                + "and one asset supplied as output.",
            )

        out_assets, _, _ = self.amount_out(in_assets, out_assets)
        out_assets.__root__[out_assets.unit()] = int(
            out_assets.__root__[out_assets.unit()] * (1 - slippage),
        )
        step = SwapExactIn.from_assets(out_assets)

        address = PlutusFullAddress.from_address(address)
        order_datum = OrderDatum(address, address, PlutusNone(), step)

        in_assets.__root__["lovelace"] = (
            in_assets["lovelace"]
            + self.batcher_fee.quantity()
            + self.deposit.quantity()
        )

        output = pycardano.TransactionOutput(
            address=self._stake_address,
            amount=asset_to_value(in_assets),
            datum_hash=order_datum.hash(),
        )

        return output, order_datum
