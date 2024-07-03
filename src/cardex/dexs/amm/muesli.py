"""Data classes and utilities for Muesli Dex.

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
from pycardano import Redeemer
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value

from cardex.backend.dbsync import get_script_from_address
from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.datums import PoolDatum
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractConstantLiquidityPoolState
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.core.errors import InvalidPoolError
from cardex.utility import Assets


@dataclass
class MuesliSometimesNone(PlutusData):
    """Represents a data structure for Muesli, sometimes with None."""

    CONSTR_ID = 0


@dataclass
class MuesliOrderConfig(PlutusData):
    """Represents configuration data for a Muesli order."""

    CONSTR_ID = 0

    full_address: PlutusFullAddress
    token_out_policy: bytes
    token_out_name: bytes
    token_in_policy: bytes
    token_in_name: bytes
    min_receive: int
    unknown: Union[MuesliSometimesNone, PlutusNone]
    in_amount: int


@dataclass
class MuesliOrderDatum(OrderDatum):
    """The order datum for MuesliSwap."""

    """Represents the datum for Muesli orders.

    Attributes:
        value (MuesliOrderConfig): Configuration data for a Muesli order.
    """

    CONSTR_ID = 0

    value: MuesliOrderConfig

    @classmethod
    def create_datum(  # noqa: PLR0913
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,
        deposit: Assets,
        address_target: Address | None = None,  # noqa: ARG003
        datum_target: PlutusData | None = None,  # noqa: ARG003
    ) -> "MuesliOrderConfig":
        """Creates an instance of MuesliOrderDatum based on provided parameters."""
        full_address = PlutusFullAddress.from_address(address_source)

        if in_assets.unit() == "lovelace":
            token_in_policy = b""
            token_in_name = b""
        else:
            token_in_policy = bytes.fromhex(in_assets.unit()[:56])
            token_in_name = bytes.fromhex(in_assets.unit()[56:])

        if out_assets.unit() == "lovelace":
            token_out_policy = b""
            token_out_name = b""
        else:
            token_out_policy = bytes.fromhex(out_assets.unit()[:56])
            token_out_name = bytes.fromhex(out_assets.unit()[56:])

        config = MuesliOrderConfig(
            full_address=full_address,
            token_in_policy=token_in_policy,
            token_in_name=token_in_name,
            token_out_policy=token_out_policy,
            token_out_name=token_out_name,
            min_receive=out_assets.quantity(),
            unknown=PlutusNone(),
            in_amount=batcher_fee.quantity() + deposit.quantity(),
        )

        return cls(value=config)

    def address_source(self) -> str:
        """Returns the source address associated with this order."""
        return self.value.full_address.to_address()

    def requested_amount(self) -> Assets:
        """Returns the requested amount based on the order configuration."""
        token_out = self.value.token_out_policy.hex() + self.value.token_out_name.hex()
        if token_out == "":
            token_out = "lovelace"  # noqa: S105
        return Assets({token_out: self.value.min_receive})

    def order_type(self) -> OrderType:
        """Returns the type of order (always 'swap' for Muesli orders)."""
        return OrderType.swap


@dataclass
class MuesliPoolDatum(PoolDatum):
    """The pool datum for MuesliSwap."""

    CONSTR_ID = 0

    asset_a: AssetClass
    asset_b: AssetClass
    lp: int
    fee: int

    def pool_pair(self) -> Assets | None:
        """Returns the pool pair assets if available."""
        return self.asset_a.assets + self.asset_b.assets


@dataclass
class PreciseFloat(PlutusData):
    """Represents a precise floating-point number."""

    CONSTR_ID = 0

    numerator: int
    denominator: int


@dataclass
class MuesliCLPoolDatum(MuesliPoolDatum):
    """Represents extended datum for Muesli constant liquidity pools."""

    upper: PreciseFloat
    lower: PreciseFloat
    price_sqrt: PreciseFloat
    unknown: int


@dataclass
class MuesliCancelRedeemer(PlutusData):
    """Represents the redeemer for canceling Muesli orders."""

    CONSTR_ID = 0


class MuesliSwapCPPState(AbstractConstantProductPoolState):
    """Represents the state of a Muesli constant product pool."""

    fee: int = 30
    _batcher = Assets(lovelace=950000)
    _deposit = Assets(lovelace=1700000)
    _test_pool: ClassVar[
        str
    ] = "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c"
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1zyq0kyrml023kwjk8zr86d5gaxrt5w8lxnah8r6m6s4jp4g3r6dxnzml343sx8jweqn4vn3fz2kj8kgu9czghx0jrsyqqktyhv",
    )
    _reference_utxo: ClassVar[UTxO | None] = None

    @classmethod
    def dex(cls) -> str:
        """Returns the name of the DEX ('MuesliSwap')."""
        return "MuesliSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Returns the order selector list."""
        return [cls._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector."""
        return PoolSelector(
            selector_type="assets",
            selector=cls.dex_policy,
        )

    @property
    def swap_forward(self) -> bool:
        """Returns whether the swap is forward."""
        return False

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Returns the reference UTxO."""
        if cls._reference_utxo is None:
            script_bytes = bytes.fromhex(
                get_script_from_address(cls._stake_address).script,
            )

            script = cls.default_script_class()(script_bytes)

            cls._reference_utxo = UTxO(
                input=TransactionInput(
                    transaction_id=TransactionId(
                        bytes.fromhex(
                            "7e4142b7a040eae45d14513000adf91ab42da33a1bd5ccffcfe851b3d93e1e5e",
                        ),
                    ),
                    index=1,
                ),
                output=TransactionOutput(
                    address=Address.decode(
                        "addr1v9p0rc57dzkz7gg97dmsns8hngsuxl956xe6myjldaug7hse4elc6",
                    ),
                    amount=Value(coin=24269610),
                    script=script,
                ),
            )

        return cls._reference_utxo

    @property
    def stake_address(self) -> Address:
        """Returns the stake address."""
        return self._stake_address

    @classmethod
    def order_datum_class(cls) -> type[MuesliOrderDatum]:
        """Returns the order datum class type."""
        return MuesliOrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[MuesliPoolDatum]:
        """Returns the pool datum class type."""
        return MuesliPoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        if self.pool_nft is None:
            error_msg = "pool_nft is None"
            raise ValueError(error_msg)
        return self.pool_nft.unit()

    @classmethod
    def dex_policy(cls) -> list[str]:
        """Returns the DEX policy list."""
        return [
            "de9b756719341e79785aa13c164e7fe68c189ed04d61c9876b2fe53f4d7565736c69537761705f414d4d",
            "ffcdbb9155da0602280c04d8b36efde35e3416567f9241aff09552694d7565736c69537761705f414d4d",
            # "f33bf12af1c23d660e29ebb0d3206b0bfc56ffd87ffafe2d36c42a454d7565736c69537761705f634c50",  # constant liquidity pools
            # "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c",  # test pool policy
        ]

    @classmethod
    def extract_dex_nft(cls, values: dict[str, Any]) -> Assets | None:
        """Extract the dex nft from the UTXO.

        Some DEXs put a DEX nft into the pool UTXO.

        This function checks to see if the DEX nft is in the UTXO if the DEX policy is
        defined.

        If the dex nft is in the values, this value is skipped because it is assumed
        that this utxo has already been parsed.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the dex nft.
        """
        dex_nft = super().extract_dex_nft(values)

        if dex_nft is not None and cls._test_pool in dex_nft:
            error_msg = "This is a test pool."
            raise InvalidPoolError(error_msg)

        return dex_nft

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Assets | None:
        """Extract the dex nft from the UTXO.

        Some DEXs put a DEX nft into the pool UTXO.

        This function checks to see if the DEX nft is in the UTXO if the DEX policy is
        defined.

        If the dex nft is in the values, this value is skipped because it is assumed
        that this utxo has already been parsed.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the dex nft.
        """
        assets = values["assets"]

        if values.get("pool_nft") is not None:
            pool_nft = Assets(root=values["pool_nft"])
        else:
            nfts = [asset for asset, quantity in assets.items() if quantity == 1]
            if len(nfts) != 1:
                error_msg = (
                    f"MuesliSwap pools must have exactly one pool nft: assets={assets}"
                )
                raise InvalidPoolError(
                    error_msg,
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        return pool_nft

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the default script class for the pool."""
        return PlutusV2Script

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        """Returns the cancel redeemer."""
        return Redeemer(MuesliCancelRedeemer())


class MuesliSwapCLPState(AbstractConstantLiquidityPoolState, MuesliSwapCPPState):
    """Represents the state of a Muesli constant liquidity pool."""

    inactive: bool = True

    @classmethod
    def dex_policy(cls) -> list[str]:
        """Returns the DEX policy list for constant liquidity pools."""
        return [
            # "de9b756719341e79785aa13c164e7fe68c189ed04d61c9876b2fe53f4d7565736c69537761705f414d4d",
            # "ffcdbb9155da0602280c04d8b36efde35e3416567f9241aff09552694d7565736c69537761705f414d4d",
            "f33bf12af1c23d660e29ebb0d3206b0bfc56ffd87ffafe2d36c42a454d7565736c69537761705f634c50",  # constant liquidity pools
            # "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c",  # test pool policy
        ]

    @classmethod
    def pool_datum_class(cls) -> type[MuesliCLPoolDatum]:
        """Returns the pool datum class type."""
        return MuesliCLPoolDatum
