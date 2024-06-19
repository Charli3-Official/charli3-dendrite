"""SundaeSwap AMM module."""

from dataclasses import dataclass
from typing import ClassVar
from typing import Union


from pycardano import Address
from pycardano import PlutusData

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.datums import PlutusPartAddress
from cardex.dataclasses.datums import PlutusScriptAddress
from cardex.dataclasses.datums import PoolDatum
from cardex.dataclasses.datums import ReceiverDatum
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError


@dataclass
class AtoB(PlutusData):
    """A to B swap direction."""

    CONSTR_ID = 0


@dataclass
class BtoA(PlutusData):
    """B to A swap direction."""

    CONSTR_ID = 1


@dataclass
class AmountOut(PlutusData):
    """Minimum amount to receive."""

    CONSTR_ID = 0
    min_receive: int


@dataclass
class SwapConfig(PlutusData):
    """Swap configuration."""

    CONSTR_ID = 0

    direction: Union[AtoB, BtoA]
    amount_in: int
    amount_out: AmountOut


@dataclass
class DepositPairQuantity(PlutusData):
    """Deposit pair quantity."""

    CONSTR_ID = 0
    amount_a: int
    amount_b: int


@dataclass
class DepositPair(PlutusData):
    """Deposit pair."""

    CONSTR_ID = 1
    assets: DepositPairQuantity


@dataclass
class DepositConfig(PlutusData):
    """Deposit configuration."""

    CONSTR_ID = 2

    deposit_pair: DepositPair


@dataclass
class WithdrawConfig(PlutusData):
    """Withdraw configuration."""

    CONSTR_ID = 1

    amount_lp: int


@dataclass
class SundaeAddressWithDatum(PlutusData):
    """SundaeSwap address with datum."""

    CONSTR_ID = 0

    address: Union[PlutusFullAddress, PlutusScriptAddress]
    datum: Union[ReceiverDatum, PlutusNone]

    @classmethod
    def from_address(cls, address: Address) -> "SundaeAddressWithDatum":
        """Create a new address with datum."""
        return cls(address=PlutusFullAddress.from_address(address), datum=PlutusNone())


@dataclass
class SundaeAddressWithDestination(PlutusData):
    """For now, destination is set to none, should be updated."""

    CONSTR_ID = 0

    address: SundaeAddressWithDatum
    destination: Union[PlutusPartAddress, PlutusNone]

    @classmethod
    def from_address(cls, address: Address) -> "SundaeAddressWithDestination":
        """Create a new address with destination."""
        null = SundaeAddressWithDatum.from_address(address)
        return cls(address=null, destination=PlutusNone())


@dataclass
class SundaeOrderDatum(OrderDatum):
    """SundaeSwap order datum."""

    ident: bytes
    address: SundaeAddressWithDestination
    fee: int
    swap: Union[DepositConfig, SwapConfig, WithdrawConfig]

    @classmethod
    def create_datum(
        cls,
        ident: bytes,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        fee: int,
    ) -> "SundaeOrderDatum":
        """Create a new order datum."""
        full_address = SundaeAddressWithDestination.from_address(address_source)
        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            direction = AtoB()
        else:
            direction = BtoA()
        swap = SwapConfig(
            direction=direction,
            amount_in=in_assets.quantity(),
            amount_out=AmountOut(min_receive=out_assets.quantity()),
        )

        return cls(ident=ident, address=full_address, fee=fee, swap=swap)

    def address_source(self) -> Address:
        """Get the source address."""
        return self.address.address.address.to_address()

    def requested_amount(self) -> Assets:
        """Get the requested amount."""
        if isinstance(self.swap, SwapConfig):
            if isinstance(self.swap.direction, AtoB):
                return Assets({"asset_b": self.swap.amount_out.min_receive})
            else:
                return Assets({"asset_a": self.swap.amount_out.min_receive})
        else:
            return Assets({})

    def order_type(self) -> OrderType:
        """Get the order type."""
        if isinstance(self.swap, SwapConfig):
            return OrderType.swap
        elif isinstance(self.swap, DepositConfig):
            return OrderType.deposit
        elif isinstance(self.swap, WithdrawConfig):
            return OrderType.withdraw


@dataclass
class LPFee(PlutusData):
    """Liquidity pool fee."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class LiquidityPoolAssets(PlutusData):
    """Liquidity pool assets."""

    CONSTR_ID = 0
    asset_a: AssetClass
    asset_b: AssetClass


@dataclass
class SundaePoolDatum(PoolDatum):
    """SundaeSwap pool datum."""

    assets: LiquidityPoolAssets
    ident: bytes
    last_swap: int
    fee: LPFee

    def pool_pair(self) -> Assets | None:
        return self.assets.asset_a.assets + self.assets.asset_b.assets


class SundaeSwapCPPState(AbstractConstantProductPoolState):
    """SundaeSwap constant product pool state."""

    fee: int
    _batcher = Assets(lovelace=2500000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1wxaptpmxcxawvr3pzlhgnpmzz3ql43n2tc8mn3av5kx0yzs09tqh8",
    )

    @classmethod
    @property
    def dex(cls) -> str:
        """Get the DEX name."""
        return "SundaeSwap"

    @classmethod
    @property
    def order_selector(self) -> list[str]:
        """Get the order selector."""
        return [self._stake_address.encode()]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        """Get the pool selector."""
        return PoolSelector(
            selector_type="addresses",
            selector=["addr1w9qzpelu9hn45pefc0xr4ac4kdxeswq7pndul2vuj59u8tqaxdznu"],
        )

    @property
    def swap_forward(self) -> bool:
        """Check if swap forwarding is enabled."""
        return False

    @property
    def stake_address(self) -> Address:
        """Get the stake address."""
        return self._stake_address

    @classmethod
    @property
    def order_datum_class(self) -> type[SundaeOrderDatum]:
        """Get the order datum class."""
        return SundaeOrderDatum

    @classmethod
    @property
    def pool_datum_class(self) -> type[SundaePoolDatum]:
        """Get the pool datum class."""
        return SundaePoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values) -> bool:
        """Skip the initialization process."""
        if "pool_nft" in values and "dex_nft" in values and "fee" in values:
            try:
                super().extract_pool_nft(values)
            except InvalidPoolError:
                raise NotAPoolError("No pool NFT found.")
            if len(values["assets"]) == 3:
                # Send the ADA token to the end
                if isinstance(values["assets"], Assets):
                    values["assets"].root["lovelace"] = values["assets"].root.pop(
                        "lovelace",
                    )
                else:
                    values["assets"]["lovelace"] = values["assets"].pop("lovelace")
            values["assets"] = Assets.model_validate(values["assets"])
            return True
        else:
            return False

    @classmethod
    def extract_pool_nft(cls, values) -> Assets:
        """Extract the pool NFT."""
        try:
            super().extract_pool_nft(values)
        except InvalidPoolError:
            if len(values["assets"]) == 0:
                raise NoAssetsError
            else:
                raise NotAPoolError("No pool NFT found.")

    @classmethod
    @property
    def pool_policy(cls) -> list[str]:
        """Get the pool policy."""
        return ["0029cb7c88c7567b63d1a512c0ed626aa169688ec980730c0473b91370"]

    @classmethod
    def post_init(cls, values):
        """Post initialization."""
        super().post_init(values)

        assets = values["assets"]
        datum = SundaePoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 2000000

        numerator = datum.fee.numerator
        denominator = datum.fee.denominator
        values["fee"] = int(numerator * 10000 / denominator)

    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> PlutusData:
        """Create a swap datum."""
        if self.swap_forward and address_target is not None:
            print(f"{self.__class__.__name__} does not support swap forwarding.")

        ident = bytes.fromhex(self.pool_nft.unit()[60:])

        return SundaeOrderDatum.create_datum(
            ident=ident,
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            fee=self.batcher_fee(in_assets=in_assets, out_assets=out_assets).quantity(),
        )
