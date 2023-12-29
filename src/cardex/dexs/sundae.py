from dataclasses import dataclass
from typing import Union

from pycardano import Address
from pycardano import PlutusData

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm_types import AbstractConstantProductPoolState
from cardex.dexs.errors import InvalidPoolError
from cardex.dexs.errors import NoAssetsError
from cardex.dexs.errors import NotAPoolError


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
    CONSTR_ID = 0

    direction: Union[AtoB, BtoA]
    amount_in: int
    amount_out: AmountOut


@dataclass
class SundaeAddressWithNone(PlutusData):
    CONSTR_ID = 0

    address: PlutusFullAddress
    null: PlutusNone

    @classmethod
    def from_address(cls, address: Address):
        return cls(address=PlutusFullAddress.from_address(address), null=PlutusNone())


@dataclass
class SundaeAddressWithDestination(PlutusData):
    """For now, destination is set to none, should be updated."""

    CONSTR_ID = 0

    address: SundaeAddressWithNone
    destination: PlutusNone

    @classmethod
    def from_address(cls, address: Address):
        null = SundaeAddressWithNone.from_address(address)
        return cls(address=null, destination=PlutusNone())


@dataclass
class SundaeOrderDatum(PlutusData):
    CONSTR_ID = 0

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

    def pool_pair(self) -> Assets | None:
        return self.assets.asset_a.assets + self.assets.asset_b.assets


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

    @property
    def swap_forward(self) -> bool:
        return False

    @property
    def stake_address(self) -> Address:
        return self._stake_address

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

    def swap_datum(
        self,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        forward_address: Address | None = None,
    ) -> PlutusData:
        if self.swap_forward and forward_address is not None:
            print(f"{self.__class__.__name__} does not support swap forwarding.")

        ident = bytes.fromhex(self.pool_nft.unit()[60:])

        return SundaeOrderDatum.create_datum(
            ident=ident,
            address=address,
            in_assets=in_assets,
            out_assets=out_assets,
            fee=self.batcher_fee.quantity(),
        )