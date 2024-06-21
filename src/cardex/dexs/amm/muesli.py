"""MuesliSwap DEX implementation."""

from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import Optional
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
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.datums import PoolDatum
from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractConstantLiquidityPoolState
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.core.errors import InvalidPoolError
from cardex.utility import Assets


@dataclass
class MuesliSometimesNone(PlutusData):
    """A dataclass that can be None."""

    CONSTR_ID = 0


@dataclass
class MuesliOrderConfig(PlutusData):
    """The order configuration for MuesliSwap."""

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

    value: MuesliOrderConfig

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
        """Create a MuesliSwap order datum."""
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
        return self.value.full_address.to_address()

    def requested_amount(self) -> Assets:
        token_out = self.value.token_out_policy.hex() + self.value.token_out_name.hex()
        if token_out == "":
            token_out = "lovelace"
        return Assets({token_out: self.value.min_receive})

    def order_type(self) -> OrderType:
        return OrderType.swap


@dataclass
class MuesliPoolDatum(PoolDatum):
    """The pool datum for MuesliSwap."""

    asset_a: AssetClass
    asset_b: AssetClass
    lp: int
    fee: int

    def pool_pair(self) -> Assets | None:
        return self.asset_a.assets + self.asset_b.assets


@dataclass
class PreciseFloat(PlutusData):
    """A precise float dataclass."""

    CONSTR_ID = 0

    numerator: int
    denominator: int


@dataclass
class MuesliCLPoolDatum(MuesliPoolDatum):
    """The pool datum for MuesliSwap constant liquidity pools."""

    upper: PreciseFloat
    lower: PreciseFloat
    price_sqrt: PreciseFloat
    unknown: int


@dataclass
class MuesliCancelRedeemer(PlutusData):
    """The cancel redeemer for MuesliSwap."""

    CONSTR_ID = 0


class MuesliSwapCPPState(AbstractConstantProductPoolState):
    """The MuesliSwap constant product pool state."""

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
    @property
    def dex(cls) -> str:
        return "MuesliSwap"

    @classmethod
    @property
    def order_selector(self) -> list[str]:
        return [self._stake_address.encode()]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=cls.dex_policy,
        )

    @property
    def swap_forward(self) -> bool:
        return False

    @classmethod
    @property
    def reference_utxo(cls) -> UTxO | None:
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
        return self._stake_address

    @classmethod
    @property
    def order_datum_class(cls) -> type[MuesliOrderDatum]:
        return MuesliOrderDatum

    @classmethod
    @property
    def pool_datum_class(cls) -> type[MuesliPoolDatum]:
        return MuesliPoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    @property
    def dex_policy(cls) -> list[str]:
        return [
            "de9b756719341e79785aa13c164e7fe68c189ed04d61c9876b2fe53f4d7565736c69537761705f414d4d",
            "ffcdbb9155da0602280c04d8b36efde35e3416567f9241aff09552694d7565736c69537761705f414d4d",
            # "f33bf12af1c23d660e29ebb0d3206b0bfc56ffd87ffafe2d36c42a454d7565736c69537761705f634c50",  # constant liquidity pools
            # "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c",  # test pool policy
        ]

    @classmethod
    def extract_dex_nft(cls, values: dict[str, Any]) -> Optional[Assets]:
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

        if cls._test_pool in dex_nft:
            raise InvalidPoolError("This is a test pool.")

        return dex_nft

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Optional[Assets]:
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

        if "pool_nft" in values:
            pool_nft = Assets(root=values["pool_nft"])
        else:
            nfts = [asset for asset, quantity in assets.items() if quantity == 1]
            if len(nfts) != 1:
                raise InvalidPoolError(
                    f"MuesliSwap pools must have exactly one pool nft: assets={assets}",
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        return pool_nft

    @classmethod
    def default_script_class(self) -> type[PlutusV1Script] | type[PlutusV2Script]:
        return PlutusV2Script

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        return Redeemer(MuesliCancelRedeemer())


class MuesliSwapCLPState(AbstractConstantLiquidityPoolState, MuesliSwapCPPState):
    """The MuesliSwap constant liquidity pool state."""

    inactive: bool = True

    @classmethod
    @property
    def dex_policy(cls) -> list[str]:
        return [
            # "de9b756719341e79785aa13c164e7fe68c189ed04d61c9876b2fe53f4d7565736c69537761705f414d4d",
            # "ffcdbb9155da0602280c04d8b36efde35e3416567f9241aff09552694d7565736c69537761705f414d4d",
            "f33bf12af1c23d660e29ebb0d3206b0bfc56ffd87ffafe2d36c42a454d7565736c69537761705f634c50",  # constant liquidity pools
            # "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c",  # test pool policy
        ]

    @classmethod
    @property
    def pool_datum_class(cls) -> type[MuesliCLPoolDatum]:
        return MuesliCLPoolDatum
