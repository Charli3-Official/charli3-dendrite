from dataclasses import dataclass
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import TransactionOutput

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.abstract_classes import AbstractConstantProductPoolState
from cardex.dexs.abstract_classes import AbstractStableSwapPoolState
from cardex.utility import InvalidLPError


@dataclass
class WingriderAssetClass(PlutusData):
    asset_a: AssetClass
    asset_b: AssetClass

    @classmethod
    def from_assets(cls, in_assets: Assets, out_assets: Assets):
        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            return cls(
                asset_a=AssetClass.from_assets(in_assets),
                asset_b=AssetClass.from_assets(out_assets),
            )
        else:
            return cls(
                asset_a=AssetClass.from_assets(out_assets),
                asset_b=AssetClass.from_assets(in_assets),
            )


@dataclass
class WingRiderOrderConfig(PlutusData):
    full_address: PlutusFullAddress
    address: bytes
    expiration: int
    assets: WingriderAssetClass

    @classmethod
    def create_config(
        cls,
        address: Address,
        expiration: int,
        in_assets: Assets,
        out_assets: Assets,
    ):
        plutus_address = PlutusFullAddress.from_address(address)
        assets = WingriderAssetClass.from_assets(
            in_assets=in_assets,
            out_assets=out_assets,
        )

        return cls(
            full_address=plutus_address,
            address=bytes.fromhex(str(address.payment.payment_part)),
            expiration=expiration,
            assets=assets,
        )


@dataclass
class AtoB(PlutusData):
    CONSTR_ID = 0


@dataclass
class BtoA(PlutusData):
    CONSTR_ID = 1


@dataclass
class WingRiderOrderDetail(PlutusData):
    direction: Union[AtoB, BtoA]
    min_receive: int

    @classmethod
    def from_assets(cls, in_assets: Assets, out_assets: Assets):
        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            return cls(direction=AtoB(), min_receive=out_assets.quantity())
        else:
            return cls(direction=BtoA(), min_receive=out_assets.quantity())


@dataclass
class WingRidersOrderDatum(PlutusData):
    config: WingRiderOrderConfig
    detail: WingRiderOrderDetail

    @classmethod
    def create_datum(
        cls,
        address: Address,
        expiration: int,
        in_assets: Assets,
        out_assets: Assets,
    ):
        config = WingRiderOrderConfig.create_config(
            address=address,
            expiration=expiration,
            in_assets=in_assets,
            out_assets=out_assets,
        )
        detail = WingRiderOrderDetail.from_assets(
            in_assets=in_assets,
            out_assets=out_assets,
        )

        return cls(config=config, detail=detail)


@dataclass
class LiquidityPoolAssets(PlutusData):
    CONSTR_ID = 0
    asset_a: AssetClass
    asset_b: AssetClass


@dataclass
class LiquidityPool(PlutusData):
    CONSTR_ID = 0
    assets: LiquidityPoolAssets
    last_swap: int
    quantity_a: int
    quantity_b: int


@dataclass
class WingRidersPoolDatum(PlutusData):
    CONSTR_ID = 0
    lp_hash: bytes
    datum: LiquidityPool


class WingRidersCPPState(AbstractConstantProductPoolState):
    fee: int = 35
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address = Address.from_primitive(
        "addr1wxr2a8htmzuhj39y2gq7ftkpxv98y2g67tg8zezthgq4jkg0a4ul4",
    )

    @classmethod
    @property
    def dex(cls) -> str:
        return "WingRiders"

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="assets",
            selector=cls.dex_policy,
        )

    @classmethod
    @property
    def order_datum_class(self) -> type[WingRidersOrderDatum]:
        return WingRidersOrderDatum

    @classmethod
    @property
    def pool_datum_class(self) -> type[WingRidersPoolDatum]:
        return WingRidersPoolDatum

    @classmethod
    @property
    def pool_policy(cls) -> str:
        return ["026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a570"]

    @classmethod
    @property
    def dex_policy(cls) -> str:
        return ["026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a5704c"]

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values) -> bool:
        if "pool_nft" in values and "dex_nft" in values:
            if len(values["assets"]) == 3:
                # Send the ADA token to the end
                values["assets"]["lovelace"] = values["assets"].pop("lovelace")
            values["assets"] = Assets.model_validate(values["assets"])
            return True
        else:
            return False

    # @classmethod
    # def extract_pool_nft(cls, values) -> Assets:
    #     if "pool_nft" in values:
    #         return Assets()

    #     assets = values["assets"]

    #     # Find the NFT that assigns the pool a unique id
    #     nfts = [
    #         asset
    #         for asset in assets
    #         if any(asset.startswith(policy) for policy in cls.pool_policy)
    #     ]
    #     if len(nfts) != 1:
    #         raise InvalidLPError(
    #             f"A pool must have one at least one LP token: {nfts}",
    #         )
    #     assets.root.pop(nfts[0])

    #     return Assets({})

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        assets = values["assets"]
        datum = WingRidersPoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 3000000

        assets.root[assets.unit(0)] -= datum.datum.quantity_a
        assets.root[assets.unit(1)] -= datum.datum.quantity_b

    def swap_tx_output(
        self,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        slippage: float = 0.005,
    ) -> tuple[TransactionOutput, WingRidersOrderDatum]:
        # Basic checks
        assert len(in_assets) == 1
        assert len(out_assets) == 1

        out_assets, _, _ = self.amount_out(in_assets, out_assets)
        out_assets.__root__[out_assets.unit()] = int(
            out_assets.__root__[out_assets.unit()] * (1 - slippage),
        )

        timeout = int((datetime.utcnow().timestamp() + 3600) * 1000)

        order_datum = WingRiderOrderDatum.create_datum(
            address=address,
            expiration=timeout,
            in_assets=in_assets,
            out_assets=out_assets,
        )

        in_assets.__root__["lovelace"] = (
            in_assets["lovelace"]
            + self.batcher_fee["lovelace"]
            + self.deposit["lovelace"]
        )

        output = TransactionOutput(
            address=self._stake_address,
            amount=asset_to_value(in_assets),
            datum_hash=order_datum.hash(),
        )

        return output, order_datum


class WingRidersSSPState(AbstractStableSwapPoolState, WingRidersCPPState):
    fee: int = 6
    _batcher = Assets(lovelace=1500000)
    _deposit = Assets(lovelace=2000000)
    _stake_address = Address.from_primitive(
        "addr1w8z7qwzszt2lqy93m3atg2axx22yq5k7yvs9rmrvuwlawts2wzadz",
    )

    @classmethod
    @property
    def pool_policy(cls) -> str:
        return ["980e8c567670d34d4ec13a0c3b6de6199f260ae5dc9dc9e867bc5c93"]

    @classmethod
    @property
    def dex_policy(cls) -> str:
        return ["980e8c567670d34d4ec13a0c3b6de6199f260ae5dc9dc9e867bc5c934c"]
