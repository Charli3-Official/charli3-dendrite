"""Data classes and utilities for Sundae Dex.

This contains data classes and utilities for handling various order and pool datums
"""
import warnings
from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import VerificationKeyHash

from cardex.backend.dbsync import get_datum_from_address
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
from cardex.dataclasses.models import PoolSelectorType
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.core.constants import THREE_VALUE
from cardex.dexs.core.constants import TWO_VALUE
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError


@dataclass
class AtoB(PlutusData):
    """Represents the direction of a swap from asset A to asset B."""

    CONSTR_ID = 0


@dataclass
class BtoA(PlutusData):
    """Represents the direction of a swap from asset B to asset A."""

    CONSTR_ID = 1


@dataclass
class AmountOut(PlutusData):
    """Represents the minimum amount to be received in a swap."""

    CONSTR_ID = 0
    min_receive: int


@dataclass
class SwapConfig(PlutusData):
    """Configuration for a swap operation."""

    CONSTR_ID = 0

    direction: Union[AtoB, BtoA]
    amount_in: int
    amount_out: AmountOut


@dataclass
class DepositPairQuantity(PlutusData):
    """Represents the quantity of asset pairs to be deposited."""

    CONSTR_ID = 0
    amount_a: int
    amount_b: int


@dataclass
class DepositPair(PlutusData):
    """Represents a pair of assets to be deposited."""

    CONSTR_ID = 1
    assets: DepositPairQuantity


@dataclass
class DepositConfig(PlutusData):
    """Configuration for a deposit operation."""

    CONSTR_ID = 2

    deposit_pair: DepositPair


@dataclass
class WithdrawConfig(PlutusData):
    """Configuration for a withdrawal operation."""

    CONSTR_ID = 1

    amount_lp: int


@dataclass
class SundaeV3PlutusNone(PlutusData):
    """Represents Plutus None."""

    CONSTR_ID = 0


@dataclass
class SundaeV3ReceiverDatumHash(PlutusData):
    """Represents receivers datum hash."""

    CONSTR_ID = 1

    datum_hash: bytes


@dataclass
class SundaeV3ReceiverInlineDatum(PlutusData):
    """Represents receivers in-line datum."""

    CONSTR_ID = 2

    datum: Any


@dataclass
class SundaeAddressWithDatum(PlutusData):
    """Represents an address with an associated datum."""

    CONSTR_ID = 0

    address: Union[PlutusFullAddress, PlutusScriptAddress]
    datum: Union[
        ReceiverDatum,
        PlutusNone,
    ]

    @classmethod
    def from_address(cls, address: Address) -> "SundaeAddressWithDatum":
        """Create a new address with datum."""
        return cls(address=PlutusFullAddress.from_address(address), datum=PlutusNone())


@dataclass
class SundaeV3AddressWithDatum(PlutusData):
    """Represents SundaeV3 address and datum object."""

    CONSTR_ID = 0

    address: Union[PlutusFullAddress, PlutusScriptAddress]
    datum: Union[
        SundaeV3PlutusNone,
        SundaeV3ReceiverDatumHash,
        SundaeV3ReceiverInlineDatum,
    ]

    @classmethod
    def from_address(cls, address: Address) -> "SundaeAddressWithDatum":
        """Creates a SundaeAddressWithDatum from an Address."""
        return cls(
            address=PlutusFullAddress.from_address(address),
            datum=SundaeV3PlutusNone(),
        )


@dataclass
class SundaeAddressWithDestination(PlutusData):
    """Represents an address with an associated destination.

    For now, the destination is set to none and should be updated.
    """

    CONSTR_ID = 0

    address: SundaeAddressWithDatum
    destination: Union[PlutusPartAddress, PlutusNone]

    @classmethod
    def from_address(cls, address: Address) -> "SundaeAddressWithDestination":
        """Creates a SundaeAddressWithDestination from an Address."""
        null = SundaeAddressWithDatum.from_address(address)
        return cls(address=null, destination=PlutusNone())


@dataclass
class SundaeOrderDatum(OrderDatum):
    """SundaeSwap order datum."""

    """Represents the datum for a SundaeSwap order."""

    CONSTR_ID = 0

    ident: bytes
    address: SundaeAddressWithDestination
    fee: int
    swap: Union[DepositConfig, SwapConfig, WithdrawConfig]

    @classmethod
    def create_datum(  # noqa: PLR0913
        cls,
        ident: bytes,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        fee: int,
    ) -> "SundaeOrderDatum":
        """Creates a SundaeOrderDatum."""
        full_address = SundaeAddressWithDestination.from_address(address_source)
        merged = in_assets + out_assets
        direction: Union[AtoB, BtoA] = (
            AtoB() if in_assets.unit() == merged.unit() else BtoA()
        )
        swap = SwapConfig(
            direction=direction,
            amount_in=in_assets.quantity(),
            amount_out=AmountOut(min_receive=out_assets.quantity()),
        )

        return cls(ident=ident, address=full_address, fee=fee, swap=swap)

    def address_source(self) -> Address:
        """Returns the source address of the order."""
        return self.address.address.address.to_address()

    def requested_amount(self) -> Assets:
        """Returns the amount requested in the order."""
        if isinstance(self.swap, SwapConfig):
            if isinstance(self.swap.direction, AtoB):
                return Assets({"asset_b": self.swap.amount_out.min_receive})
            return Assets({"asset_a": self.swap.amount_out.min_receive})
        return Assets({})

    def order_type(self) -> OrderType:
        """Returns the type of the order."""
        if isinstance(self.swap, SwapConfig):
            return OrderType.swap
        if isinstance(self.swap, DepositConfig):
            return OrderType.deposit
        if isinstance(self.swap, WithdrawConfig):
            return OrderType.withdraw
        return None


@dataclass
class SwapV3Config(PlutusData):
    """Swap V3 configurations."""

    CONSTR_ID = 1
    in_value: list[Union[int, bytes]]
    out_value: list[Union[int, bytes]]


@dataclass
class DepositV3Config(PlutusData):
    """Deposit V3 configurations."""

    CONSTR_ID = 2
    values: list[list[Union[int, bytes]]]


@dataclass
class WithdrawV3Config(PlutusData):
    """Withdraw V3 configurations."""

    CONSTR_ID = 3
    in_value: list[Union[int, bytes]]


@dataclass
class DonateV3Config(PlutusData):
    """Donate V3 configurations."""

    CONSTR_ID = 4
    in_value: list[Union[int, bytes]]
    out_value: list[Union[int, bytes]]


@dataclass
class Ident(PlutusData):
    """Ident."""

    CONSTR_ID = 0
    payload: bytes


@dataclass
class SundaeV3OrderDatum(OrderDatum):
    """Represents a Sundae V3 order datum for transactions."""

    CONSTR_ID = 0

    ident: Ident
    owner: PlutusPartAddress
    max_protocol_fee: int
    destination: SundaeV3AddressWithDatum
    swap: Union[
        DepositV3Config,
        WithdrawV3Config,
        # ZapInV3Config,
        # ZapOutV3Config,
        DonateV3Config,
        SwapV3Config,
    ]
    extension: bytes

    @classmethod
    def create_datum(  # noqa: PLR0913
        cls,
        ident: bytes,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        fee: int,
    ) -> "SundaeV3OrderDatum":
        """Create a Sundae V3 order datum based on provided parameters.

        Args:
            ident (bytes): The identifier of the order datum.
            address_source (Address): The source address for the owner.
            in_assets (Assets): Input assets for the transaction.
            out_assets (Assets): Output assets for the transaction.
            fee (int): Maximum protocol fee allowed for the order.

        Returns:
            SundaeV3OrderDatum: A newly created Sundae V3 order datum instance.
        """
        full_address = SundaeV3AddressWithDatum.from_address(address_source)
        merged = in_assets + out_assets
        direction: Union[AtoB, BtoA] = (
            AtoB() if in_assets.unit() == merged.unit() else BtoA()
        )
        _ = SwapConfig(
            direction=direction,
            amount_in=in_assets.quantity(),
            amount_out=AmountOut(min_receive=out_assets.quantity()),
        )

        if in_assets.unit() == "lovelace":
            in_policy = in_name = ""
        else:
            in_policy = in_assets.unit()[:56]
            in_name = in_assets.unit()[56:]

        if out_assets.unit() == "lovelace":
            out_policy = out_name = ""
        else:
            out_policy = out_assets.unit()[:56]
            out_name = out_assets.unit()[56:]

        in_value: list[int | bytes] = [
            bytes.fromhex(in_policy),
            bytes.fromhex(in_name),
            in_assets.quantity(),
        ]
        out_value: list[int | bytes] = [
            bytes.fromhex(out_policy),
            bytes.fromhex(out_name),
            out_assets.quantity(),
        ]

        return cls(
            ident=Ident(payload=ident),
            owner=PlutusPartAddress(address=address_source.staking_part.payload),
            max_protocol_fee=fee,
            destination=full_address,
            swap=SwapV3Config(in_value=in_value, out_value=out_value),
            extension=PlutusData().to_cbor(),
        )

    def address_source(self) -> Address:
        """Return the address source associated with the owner of the order datum."""
        return Address(staking_part=VerificationKeyHash(self.owner.address))

    def requested_amount(self) -> Assets:
        """Return the requested amount based on the swap configuration, if available."""
        if isinstance(self.swap, SwapV3Config):
            out_value_0 = self.swap.out_value[0]
            out_value_1 = self.swap.out_value[1]

            if isinstance(out_value_0, bytes) and isinstance(out_value_1, bytes):
                return Assets(
                    {
                        (out_value_0 + out_value_1).hex(): self.swap.out_value[2],
                    },
                )
        return Assets({})

    def order_type(self) -> OrderType:
        """Type of order which either swap, depoist, withdraw. or none.

        Returns:
            OrderType: The order type.
        """
        if isinstance(self.swap, SwapV3Config):
            return OrderType.swap
        if isinstance(self.swap, DepositV3Config):
            return OrderType.deposit
        if isinstance(self.swap, WithdrawV3Config):
            return OrderType.withdraw
        error_msg = "Unknown order type. Expected one of: SwapV3Config, DepositV3Config, WithdrawV3Config."
        raise ValueError(error_msg)


@dataclass
class LPFee(PlutusData):
    """Represents the fee structure for a liquidity pool."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class LiquidityPoolAssets(PlutusData):
    """Represents the assets in a liquidity pool."""

    CONSTR_ID = 0
    asset_a: AssetClass
    asset_b: AssetClass


@dataclass
class SundaePoolDatum(PoolDatum):
    """Represents the datum for a SundaeSwap liquidity pool."""

    CONSTR_ID = 0
    assets: LiquidityPoolAssets
    ident: bytes
    last_swap: int
    fee: LPFee

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the liquidity pool."""
        return self.assets.asset_a.assets + self.assets.asset_b.assets


@dataclass
class SundaeV3PoolDatum(PlutusData):
    """Represents the datum structure for a SundaeSwap V3 pool."""

    CONSTR_ID = 0
    ident: bytes
    assets: list[list[bytes]]
    circulation_lp: int
    bid_fees_per_10_thousand: int
    ask_fees_per_10_thousand: int
    fee_manager: Union[PlutusNone, Any]
    market_open: int  # time in milliseconds
    protocol_fees: int

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the pool."""
        assets = {}
        for asset in self.assets:
            assets[asset[0].hex() + asset[1].hex()] = 0
        if "" in assets:
            assets.pop("")
            assets["lovelace"] = 0
        return Assets(**assets)


@dataclass
class SundaeV3Settings(PlutusData):
    """Represents Sundae V3 Settings."""

    CONSTR_ID = 0
    settings_admin: Any  # NativeScript
    metadata_admin: PlutusFullAddress
    treasury_admin: Any  # NativeScript
    treasury_address: PlutusFullAddress
    treasury_allowance: list[int]
    authorized_scoopers: Union[PlutusNone, Any]  # list[PlutusPartAddress]]
    authorized_staking_keys: list[Any]
    base_fee: int
    simple_fee: int
    strategy_fee: int
    pool_creation_fee: int
    extensions: Any


class SundaeSwapCPPState(AbstractConstantProductPoolState):
    """Represents the state of a SundaeSwap constant product pool."""

    fee: int
    _batcher = Assets(lovelace=2500000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1wxaptpmxcxawvr3pzlhgnpmzz3ql43n2tc8mn3av5kx0yzs09tqh8",
    )

    @classmethod
    def dex(cls) -> str:
        """Get the DEX name."""
        return "SundaeSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Get the order selector."""
        return [cls._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Get the pool selector."""
        return PoolSelector(
            selector_type=PoolSelectorType.address,
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
    def order_datum_class(cls) -> type[SundaeOrderDatum]:
        """Get the order datum class."""
        return SundaeOrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[SundaePoolDatum]:
        """Get the pool datum class."""
        return SundaePoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        if self.pool_nft is None:
            error_msg = "pool_nft is None"
            raise ValueError(error_msg)
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values: dict[str, Any]) -> bool:
        """Skip the initialization process."""
        if "pool_nft" in values and "dex_nft" in values and "fee" in values:
            try:
                super().extract_pool_nft(values)
            except InvalidPoolError as err:
                error_msg = "No pool NFT found."
                raise NotAPoolError(error_msg) from err
            if len(values["assets"]) == THREE_VALUE:
                # Send the ADA token to the end
                if isinstance(values["assets"], Assets):
                    values["assets"].root["lovelace"] = values["assets"].root.pop(
                        "lovelace",
                    )
                else:
                    values["assets"]["lovelace"] = values["assets"].pop("lovelace")
            values["assets"] = Assets.model_validate(values["assets"])
            return True
        return False

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Assets | None:
        """Extract the pool NFT."""
        try:
            return super().extract_pool_nft(values)
        except InvalidPoolError as err:
            if len(values["assets"]) == 0:
                raise NoAssetsError from err
            error_msg = "No pool NFT found."
            raise NotAPoolError(error_msg) from err

    @classmethod
    def pool_policy(cls) -> list[str]:
        """Get the pool policy."""
        return ["0029cb7c88c7567b63d1a512c0ed626aa169688ec980730c0473b91370"]

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
        datum = SundaePoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == TWO_VALUE:
            assets.root[assets.unit(0)] -= 2000000

        numerator = datum.fee.numerator
        denominator = datum.fee.denominator
        values["fee"] = int(numerator * 10000 / denominator)
        return values

    def swap_datum(  # noqa: PLR0913
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,  # noqa: ARG002
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,  # noqa: ARG002
    ) -> PlutusData:
        """Create a swap datum."""
        if self.swap_forward and address_target is not None:
            warnings.warn(
                f"{self.__class__.__name__} does not support swap forwarding.",
                stacklevel=2,
            )
        if self.pool_nft is None:
            error_msg = "Pool NFT cannot be None"
            raise ValueError(error_msg)

        ident = bytes.fromhex(self.pool_nft.unit()[60:])

        return SundaeOrderDatum.create_datum(
            ident=ident,
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            fee=self.batcher_fee(in_assets=in_assets, out_assets=out_assets).quantity(),
        )


class SundaeSwapV3CPPState(AbstractConstantProductPoolState):
    """Represents the state of a constant product pool for SundaeSwap V3."""

    fee: int = 30
    _batcher = Assets(lovelace=1000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1z8ax5k9mutg07p2ngscu3chsauktmstq92z9de938j8nqa7zcka2k2tsgmuedt4xl2j5awftvqzmmv3vs2yduzqxfcmsyun6n3",
    )

    @classmethod
    def dex(cls) -> str:
        """Returns dex name."""
        return "SundaeSwap"

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the default script class for the pool."""
        return PlutusV2Script

    @classmethod
    def order_selector(cls) -> list[str]:
        """Returns: The order selector list."""
        return [(cls._stake_address).encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector for the DEX."""
        return PoolSelector(
            selector_type=PoolSelectorType.address,
            selector=[
                "addr1x8srqftqemf0mjlukfszd97ljuxdp44r372txfcr75wrz26rnxqnmtv3hdu2t6chcfhl2zzjh36a87nmd6dwsu3jenqsslnz7e",
            ],
        )

    @classmethod
    def pool_policy(cls) -> list[str]:
        """Returns pool policy."""
        return ["e0302560ced2fdcbfcb2602697df970cd0d6a38f94b32703f51c312b"]

    @property
    def swap_forward(self) -> bool:
        """Indicates if swap forwarding is supported."""
        return False

    @property
    def stake_address(self) -> Address:
        """Returns the stake address for the DEX."""
        return self._stake_address

    @classmethod
    def order_datum_class(cls) -> type[SundaeV3OrderDatum]:
        """Returns the class for the order datum."""
        return SundaeV3OrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[SundaeV3PoolDatum]:
        """Returns the class for the pool datum."""
        return SundaeV3PoolDatum

    @property
    def pool_id(self) -> str:
        """Returns a unique identifier for the pool."""
        if self.pool_nft is None:
            error_msg = "pool_nft is None"
            raise ValueError(error_msg)
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values: dict[str, Any]) -> bool:
        """Determines if initialization should be skipped based on the provided values."""
        if "pool_nft" in values and "dex_nft" in values and "fee" in values:
            try:
                super().extract_pool_nft(values)
            except InvalidPoolError as err:
                error_msg = "No pool NFT found."
                raise NotAPoolError(error_msg) from err
            if len(values["assets"]) == THREE_VALUE:
                # Send the ADA token to the end
                if isinstance(values["assets"], Assets):
                    values["assets"].root["lovelace"] = values["assets"].root.pop(
                        "lovelace",
                    )
                else:
                    values["assets"]["lovelace"] = values["assets"].pop("lovelace")
            values["assets"] = Assets.model_validate(values["assets"])
            return True
        return False

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Assets | None:
        """Extracts the pool NFT from the provided values."""
        try:
            return super().extract_pool_nft(values)
        except InvalidPoolError as err:
            if len(values["assets"]) == 0:
                raise NoAssetsError from err
            error_msg = "No pool NFT found."
            raise NotAPoolError(error_msg) from err

    def batcher_fee(
        self,
        in_assets: Assets | None = None,  # noqa: ARG002
        out_assets: Assets | None = None,  # noqa: ARG002
        extra_assets: Assets | None = None,  # noqa: ARG002
    ) -> Assets:
        """Calculates the batcher fee based on settings."""
        settings = get_datum_from_address(
            Address.decode(
                "addr1w9680rk7hkue4e0zkayyh47rxqpg9gzx445mpha3twge75sku2mg0",
            ),
        )

        datum = SundaeV3Settings.from_cbor(settings.datum_cbor)
        return Assets(lovelace=datum.simple_fee + datum.base_fee)

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
        datum = SundaeV3PoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == TWO_VALUE:
            assets.root[assets.unit(0)] -= datum.protocol_fees

        values["fee"] = datum.bid_fees_per_10_thousand
        return values

    def swap_datum(  # noqa: PLR0913
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,  # noqa: ARG002
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,  # noqa: ARG002
    ) -> PlutusData:
        """Creates the datum for a swap operation."""
        if self.swap_forward and address_target is not None:
            error_msg = f"{self.__class__.__name__} does not support swap forwarding."
            raise ValueError(error_msg)
        if self.pool_nft is None:
            error_msg = "Pool NFT cannot be None"
            raise ValueError(error_msg)

        ident = bytes.fromhex(self.pool_nft.unit()[64:])

        return SundaeV3OrderDatum.create_datum(
            ident=ident,
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            fee=self.batcher_fee(in_assets=in_assets, out_assets=out_assets).quantity(),
        )
