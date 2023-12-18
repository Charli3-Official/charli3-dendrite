from dataclasses import dataclass
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import TransactionOutput

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.abstract_classes import AbstractConstantProductPoolState
from cardex.utility import InvalidPoolError
from cardex.utility import NoAssetsError
from cardex.utility import NotAPoolError


@dataclass
class AtoB(PlutusData):
    CONSTR_ID = 0


@dataclass
class BtoA(PlutusData):
    CONSTR_ID = 1


@dataclass
class AmountOut(PlutusData):
    CONSTR_ID = 0
    min_receive: int


@dataclass
class SwapConfig(PlutusData):
    direction: Union[AtoB, BtoA]
    amount_in: int
    amount_out: AmountOut


@dataclass
class SundaeAddressWithNone(PlutusData):
    address: PlutusFullAddress
    null: PlutusNone

    @classmethod
    def from_address(cls, address: Address):
        return cls(address=PlutusFullAddress.from_address(address), null=PlutusNone())


@dataclass
class SundaeAddressWithDestination(PlutusData):
    """For now, destination is set to none, should be updated."""

    address: SundaeAddressWithNone
    destination: PlutusNone

    @classmethod
    def from_address(cls, address: Address):
        null = SundaeAddressWithNone.from_address(address)
        return cls(address=null, destination=PlutusNone())


@dataclass
class SundaeOrderDatum(PlutusData):
    ident: bytes
    address: SundaeAddressWithDestination
    fee: int
    swap: SwapConfig

    @classmethod
    def create_datum(
        cls,
        ident: bytes,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        fee: int,
    ):
        full_address = SundaeAddressWithDestination.from_address(address)
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


@dataclass
class LPFee(PlutusData):
    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class LiquidityPoolAssets(PlutusData):
    CONSTR_ID = 0
    asset_a: AssetClass
    asset_b: AssetClass


@dataclass
class SundaePoolDatum(PlutusData):
    CONSTR_ID = 0
    assets: LiquidityPoolAssets
    ident: bytes
    last_swap: int
    fee: LPFee


class SundaeSwapCPPState(AbstractConstantProductPoolState):
    fee: int
    _batcher = Assets(lovelace=2500000)
    _deposit = Assets(lovelace=2000000)
    _stake_address = Address.from_primitive(
        "addr1wxaptpmxcxawvr3pzlhgnpmzz3ql43n2tc8mn3av5kx0yzs09tqh8",
    )

    @classmethod
    @property
    def dex(cls) -> str:
        return "SundaeSwap"

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="addresses",
            selector=["addr1w9qzpelu9hn45pefc0xr4ac4kdxeswq7pndul2vuj59u8tqaxdznu"],
        )

    @classmethod
    @property
    def order_datum_class(self) -> type[SundaeOrderDatum]:
        return SundaeOrderDatum

    @classmethod
    @property
    def pool_datum_class(self) -> type[SundaePoolDatum]:
        return SundaePoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values) -> bool:
        if "pool_nft" in values and "dex_nft" in values and "fee" in values:
            try:
                super().extract_pool_nft(values)
            except InvalidPoolError:
                raise NotAPoolError("No pool NFT found.")
            if len(values["assets"]) == 3:
                # Send the ADA token to the end
                values["assets"]["lovelace"] = values["assets"].pop("lovelace")
            values["assets"] = Assets.model_validate(values["assets"])
            return True
        else:
            return False

    @classmethod
    def extract_pool_nft(cls, values) -> Assets:
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
        return ["0029cb7c88c7567b63d1a512c0ed626aa169688ec980730c0473b91370"]

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        assets = values["assets"]
        datum = SundaePoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 2000000

        numerator = datum.fee.numerator
        denominator = datum.fee.denominator
        values["fee"] = int(numerator * 10000 / denominator)

    def swap_tx_output(
        self,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        slippage: float = 0.005,
    ) -> tuple[TransactionOutput, SundaeOrderDatum]:
        # Basic checks
        assert len(in_assets) == 1
        assert len(out_assets) == 1

        out_assets, _, _ = self.amount_out(in_assets, out_assets)
        out_assets.__root__[out_assets.unit()] = int(
            out_assets.__root__[out_assets.unit()] * (1 - slippage),
        )

        pool = self.get_pool_from_assets(in_assets + out_assets)
        ident = bytes.fromhex(pool.pool_nft.unit()[60:])

        order_datum = SundaeOrderDatum.create_datum(
            ident=ident,
            address=address,
            in_assets=in_assets,
            out_assets=out_assets,
            fee=pool.batcher_fee.quantity(),
        )

        in_assets.__root__["lovelace"] = (
            in_assets["lovelace"]
            + self.batcher_fee["lovelace"]
            + self.deposit["lovelace"]
        )

        output = pycardano.TransactionOutput(
            address=STAKE_ORDER.address,
            amount=asset_to_value(in_assets),
            datum_hash=order_datum.hash(),
        )

        return output, order_datum
