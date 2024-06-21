"""WingRiders DEX implementation."""

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from typing import ClassVar
from typing import Union

from pycardano import Address
from pycardano import PlutusData

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.datums import PoolDatum
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.amm.amm_types import AbstractStableSwapPoolState
from cardex.dexs.core.errors import NotAPoolError


@dataclass
class WingriderAssetClass(PlutusData):
    """Encode a pair of assets for the WingRiders DEX."""

    CONSTR_ID = 0

    asset_a: AssetClass
    asset_b: AssetClass

    @classmethod
    def from_assets(cls, in_assets: Assets, out_assets: Assets):
        """Create a WingRiderAssetClass from a pair of assets."""
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
class RewardPlutusPartAddress(PlutusData):
    """Encode a plutus address part (i.e. payment, stake, etc)."""

    CONSTR_ID = 1
    address: bytes


@dataclass
class RewardPlutusFullAddress(PlutusFullAddress):
    """A full address, including payment and staking keys."""

    CONSTR_ID = 0

    payment: RewardPlutusPartAddress


@dataclass
class WingRiderOrderConfig(PlutusData):
    """Configuration for a WingRiders order."""

    CONSTR_ID = 0

    full_address: Union[PlutusFullAddress, RewardPlutusFullAddress]
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
        """Create a WingRiders order configuration."""
        plutus_address = PlutusFullAddress.from_address(address)
        assets = WingriderAssetClass.from_assets(
            in_assets=in_assets,
            out_assets=out_assets,
        )

        return cls(
            full_address=plutus_address,
            address=bytes.fromhex(str(address.payment_part)),
            expiration=expiration,
            assets=assets,
        )


@dataclass
class AtoB(PlutusData):
    """A to B."""

    CONSTR_ID = 0


@dataclass
class BtoA(PlutusData):
    """B to A."""

    CONSTR_ID = 1


@dataclass
class WingRidersOrderDetail(PlutusData):
    """WingRiders order detail."""

    CONSTR_ID = 0

    direction: Union[AtoB, BtoA]
    min_receive: int

    @classmethod
    def from_assets(cls, in_assets: Assets, out_assets: Assets):
        """Create a WingRidersOrderDetail from a pair of assets."""
        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            return cls(direction=AtoB(), min_receive=out_assets.quantity())
        else:
            return cls(direction=BtoA(), min_receive=out_assets.quantity())


@dataclass
class WingRidersDepositDetail(PlutusData):
    """WingRiders deposit detail."""

    CONSTR_ID = 1

    min_lp_receive: int


@dataclass
class WingRidersWithdrawDetail(PlutusData):
    """WingRiders withdraw detail."""

    CONSTR_ID = 2

    min_amount_a: int
    min_amount_b: int


@dataclass
class WingRidersMaybeFeeClaimDetail(PlutusData):
    """WingRiders maybe fee claim detail."""

    CONSTR_ID = 3


@dataclass
class WingRidersStakeRewardDetail(PlutusData):
    """WingRiders stake reward detail."""

    CONSTR_ID = 4


@dataclass
class WingRidersOrderDatum(OrderDatum):
    """WingRiders order datum."""

    config: WingRiderOrderConfig
    detail: Union[
        WingRidersDepositDetail,
        WingRidersMaybeFeeClaimDetail,
        WingRidersStakeRewardDetail,
        WingRidersOrderDetail,
        WingRidersWithdrawDetail,
    ]

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
        """Create a WingRiders order datum."""
        timeout = int(((datetime.utcnow() + timedelta(days=360)).timestamp()) * 1000)

        config = WingRiderOrderConfig.create_config(
            address=address_source,
            expiration=timeout,
            in_assets=in_assets,
            out_assets=out_assets,
        )
        detail = WingRidersOrderDetail.from_assets(
            in_assets=in_assets,
            out_assets=out_assets,
        )

        return cls(config=config, detail=detail)

    def address_source(self) -> Address:
        return self.config.full_address.to_address()

    def requested_amount(self) -> Assets:
        if isinstance(self.detail, WingRidersDepositDetail):
            return Assets({"lp": self.detail.min_lp_receive})
        elif isinstance(self.detail, WingRidersOrderDetail):
            if isinstance(self.detail.direction, BtoA):
                return Assets(
                    {self.config.assets.asset_a.assets.unit(): self.detail.min_receive},
                )
            else:
                return Assets(
                    {self.config.assets.asset_b.assets.unit(): self.detail.min_receive},
                )
        elif isinstance(self.detail, WingRidersWithdrawDetail):
            return Assets(
                {
                    self.config.assets.asset_a.assets.unit(): self.detail.min_amount_a,
                    self.config.assets.asset_b.assets.unit(): self.detail.min_amount_b,
                },
            )
        elif isinstance(self.detail, WingRidersMaybeFeeClaimDetail):
            return Assets({})

    def order_type(self) -> OrderType:
        if isinstance(self.detail, WingRidersOrderDetail):
            return OrderType.swap
        elif isinstance(self.detail, WingRidersDepositDetail):
            return OrderType.deposit
        elif isinstance(self.detail, WingRidersWithdrawDetail):
            return OrderType.withdraw
        if isinstance(self.detail, WingRidersMaybeFeeClaimDetail):
            return OrderType.withdraw


@dataclass
class LiquidityPoolAssets(PlutusData):
    """Encode a pair of assets for the WingRiders DEX."""

    CONSTR_ID = 0
    asset_a: AssetClass
    asset_b: AssetClass


@dataclass
class LiquidityPool(PlutusData):
    """Encode a liquidity pool for the WingRiders DEX."""

    CONSTR_ID = 0
    assets: LiquidityPoolAssets
    last_swap: int
    quantity_a: int
    quantity_b: int


@dataclass
class WingRidersPoolDatum(PoolDatum):
    """WingRiders pool datum."""

    lp_hash: bytes
    datum: LiquidityPool

    def pool_pair(self) -> Assets | None:
        return self.datum.assets.asset_a.assets + self.datum.assets.asset_b.assets


class WingRidersCPPState(AbstractConstantProductPoolState):
    """WingRiders CPP state."""

    fee: int = 35
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1wxr2a8htmzuhj39y2gq7ftkpxv98y2g67tg8zezthgq4jkg0a4ul4",
    )

    @classmethod
    @property
    def dex(cls) -> str:
        return "WingRiders"

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

    @property
    def stake_address(self) -> Address:
        return self._stake_address

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
            if cls.dex_policy[0] not in values["dex_nft"]:
                raise NotAPoolError("Invalid DEX NFT")
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
    def post_init(cls, values):
        super().post_init(values)

        assets = values["assets"]
        datum = WingRidersPoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 3000000

        assets.root[assets.unit(0)] -= datum.datum.quantity_a
        assets.root[assets.unit(1)] -= datum.datum.quantity_b

    def deposit(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
    ):
        merged_assets = in_assets + out_assets
        if "lovelace" in merged_assets:
            return Assets(lovelace=4000000) - self.batcher_fee(
                in_assets=in_assets,
                out_assets=out_assets,
            )
        else:
            return self._deposit

    def batcher_fee(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
        extra_assets: Assets | None = None,
    ):
        merged_assets = in_assets + out_assets
        if "lovelace" in merged_assets:
            if merged_assets["lovelace"] <= 250000000:
                return Assets(lovelace=850000)
            elif merged_assets["lovelace"] <= 500000000:
                return Assets(lovelace=1500000)
        return self._batcher


class WingRidersSSPState(AbstractStableSwapPoolState, WingRidersCPPState):
    """WingRiders SSP state."""

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
