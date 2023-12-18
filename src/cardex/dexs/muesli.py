from dataclasses import dataclass
from typing import Any
from typing import Optional

from pycardano import Address
from pycardano import PlutusData
from pycardano import TransactionOutput

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.abstract_classes import AbstractConstantLiquidityPoolState
from cardex.dexs.abstract_classes import AbstractConstantProductPoolState
from cardex.utility import Assets
from cardex.utility import InvalidPoolError


@dataclass
class MuesliOrderConfig(PlutusData):
    CONSTR_ID = 0

    full_address: PlutusFullAddress
    token_out_policy: bytes
    token_out_name: bytes
    token_in_policy: bytes
    token_in_name: bytes
    min_receive: int
    unknown: PlutusNone
    in_amount: int


@dataclass
class MuesliOrderDatum(PlutusData):
    CONSTR_ID = 0

    value: MuesliOrderConfig

    @classmethod
    def create_datum(
        cls,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        fee=Assets,
    ):
        full_address = PlutusFullAddress.from_address(address)

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
            in_amount=fee,
        )

        return cls(value=config)


@dataclass
class MuesliPoolDatum(PlutusData):
    CONSTR_ID = 0

    asset_a: AssetClass
    asset_b: AssetClass
    lp: int
    fee: int


@dataclass
class PreciseFloat(PlutusData):
    CONSTR_ID = 0

    numerator: int
    denominator: int


@dataclass
class MuesliCLPoolDatum(MuesliPoolDatum):
    upper: PreciseFloat
    lower: PreciseFloat
    price_sqrt: PreciseFloat
    unknown: int


class MuesliSwapCPPState(AbstractConstantProductPoolState):
    fee: int = 30
    _batcher = Assets(lovelace=950000)
    _deposit = Assets(lovelace=1700000)
    _test_pool = "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c"
    _stake_address = Address.from_primitive(
        "addr1zyq0kyrml023kwjk8zr86d5gaxrt5w8lxnah8r6m6s4jp4g3r6dxnzml343sx8jweqn4vn3fz2kj8kgu9czghx0jrsyqqktyhv",
    )

    @classmethod
    @property
    def dex(cls) -> str:
        return "MuesliSwap"

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=cls.dex_policy,
        )

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

    def swap_tx_output(
        self,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        slippage: float = 0.005,
    ) -> tuple[TransactionOutput, MuesliOrderDatum]:
        # Basic checks
        assert len(in_assets) == 1
        assert len(out_assets) == 1

        out_assets, _, _ = self.amount_out(in_assets, out_assets)
        out_assets.__root__[out_assets.unit()] = int(
            out_assets.__root__[out_assets.unit()] * (1 - slippage),
        )

        order_datum = MuesliOrder.create_datum(
            address=address,
            in_assets=in_assets,
            out_assets=out_assets,
            fee=self.batcher_fee["lovelace"] + self.deposit["lovelace"],
        )

        in_assets.__root__["lovelace"] = (
            in_assets["lovelace"]
            + self.batcher_fee["lovelace"]
            + self.deposit["lovelace"]
        )

        output = pycardano.TransactionOutput(
            address=self._stake_address,
            amount=asset_to_value(in_assets),
            datum_hash=order_datum.hash(),
        )

        return output, order_datum


class MuesliSwapCLPState(AbstractConstantLiquidityPoolState, MuesliSwapCPPState):
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
