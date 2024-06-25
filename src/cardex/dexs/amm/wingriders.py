"""Data classes and utilities for Windgriders Dex.

This contains data classes and utilities for handling various order and pool datums
"""
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from typing import Any
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
from cardex.dexs.core.constants import BATCHER_FEE_THRESHOLD_HIGH
from cardex.dexs.core.constants import BATCHER_FEE_THRESHOLD_LOW
from cardex.dexs.core.constants import THREE_VALUE
from cardex.dexs.core.constants import ZERO_VALUE
from cardex.dexs.core.errors import NotAPoolError


@dataclass
class WingriderAssetClass(PlutusData):
    """Represents a pair of asset classes in WingRiders."""

    CONSTR_ID = 0

    asset_a: AssetClass
    asset_b: AssetClass

    @classmethod
    def from_assets(
        cls,
        in_assets: Assets,
        out_assets: Assets,
    ) -> "WingriderAssetClass":
        """Creates a WingriderAssetClass instance from given input and output assets."""
        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            return cls(
                asset_a=AssetClass.from_assets(in_assets),
                asset_b=AssetClass.from_assets(out_assets),
            )
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
    ) -> "WingRiderOrderConfig":
        """Creates a WingRiderOrderConfig instance."""
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
    """Represents a swap direction from asset A to asset B."""

    CONSTR_ID = 0


@dataclass
class BtoA(PlutusData):
    """Represents a swap direction from asset B to asset A."""

    CONSTR_ID = 1


@dataclass
class WingRidersOrderDetail(PlutusData):
    """Details for a WingRiders order."""

    CONSTR_ID = 0

    direction: Union[AtoB, BtoA]
    min_receive: int

    @classmethod
    def from_assets(
        cls,
        in_assets: Assets,
        out_assets: Assets,
    ) -> "WingRidersOrderDetail":
        """Creates a WingRidersOrderDetail instance from given input & output assets."""
        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            return cls(direction=AtoB(), min_receive=out_assets.quantity())
        return cls(direction=BtoA(), min_receive=out_assets.quantity())


@dataclass
class WingRidersDepositDetail(PlutusData):
    """Details for a WingRiders deposit."""

    CONSTR_ID = 1

    min_lp_receive: int


@dataclass
class WingRidersWithdrawDetail(PlutusData):
    """Details for a WingRiders withdrawal."""

    CONSTR_ID = 2

    min_amount_a: int
    min_amount_b: int


@dataclass
class WingRidersMaybeFeeClaimDetail(PlutusData):
    """Details for a WingRiders fee claim."""

    CONSTR_ID = 3


@dataclass
class WingRidersStakeRewardDetail(PlutusData):
    """Details for a WingRiders stake reward."""

    CONSTR_ID = 4


@dataclass
class WingRidersOrderDatum(OrderDatum):
    """Datum for a WingRiders order."""

    CONSTR_ID = 0

    config: WingRiderOrderConfig
    detail: Union[
        WingRidersDepositDetail,
        WingRidersMaybeFeeClaimDetail,
        WingRidersStakeRewardDetail,
        WingRidersOrderDetail,
        WingRidersWithdrawDetail,
    ]

    @classmethod
    def create_datum(  # noqa: PLR0913
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,  # noqa: ARG003
        deposit: Assets,  # noqa: ARG003
        address_target: Address | None = None,  # noqa: ARG003
        datum_target: PlutusData | None = None,  # noqa: ARG003
    ) -> "WingRidersOrderDatum":
        """Creates a WingRidersOrderDatum instance."""
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
        """Returns the source address of the order."""
        return self.config.full_address.to_address()

    def requested_amount(self) -> Assets:
        """Returns the requested amount for the order."""
        if isinstance(self.detail, WingRidersDepositDetail):
            return Assets({"lp": self.detail.min_lp_receive})
        if isinstance(self.detail, WingRidersOrderDetail):
            if isinstance(self.detail.direction, BtoA):
                return Assets(
                    {self.config.assets.asset_a.assets.unit(): self.detail.min_receive},
                )
            return Assets(
                {self.config.assets.asset_b.assets.unit(): self.detail.min_receive},
            )
        if isinstance(self.detail, WingRidersWithdrawDetail):
            return Assets(
                {
                    self.config.assets.asset_a.assets.unit(): self.detail.min_amount_a,
                    self.config.assets.asset_b.assets.unit(): self.detail.min_amount_b,
                },
            )
        if isinstance(self.detail, WingRidersMaybeFeeClaimDetail):
            return Assets({})
        error_msg = "Invalid detail type for requested_amount"
        raise ValueError(error_msg)

    def order_type(self) -> OrderType:
        """Returns the type of the order."""
        if isinstance(self.detail, WingRidersOrderDetail):
            return OrderType.swap
        if isinstance(self.detail, WingRidersDepositDetail):
            return OrderType.deposit
        if isinstance(self.detail, WingRidersWithdrawDetail):
            return OrderType.withdraw
        if isinstance(self.detail, WingRidersMaybeFeeClaimDetail):
            return OrderType.withdraw
        error_msg = "Invalid detail type for order_type"
        raise ValueError(error_msg)


@dataclass
class LiquidityPoolAssets(PlutusData):
    """Represents the assets in a liquidity pool."""

    CONSTR_ID = 0
    asset_a: AssetClass
    asset_b: AssetClass


@dataclass
class LiquidityPool(PlutusData):
    """Represents a liquidity pool."""

    CONSTR_ID = 0
    assets: LiquidityPoolAssets
    last_swap: int
    quantity_a: int
    quantity_b: int


@dataclass
class WingRidersPoolDatum(PoolDatum):
    """Datum for a WingRiders liquidity pool."""

    CONSTR_ID = 0
    lp_hash: bytes
    datum: LiquidityPool

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the liquidity pool."""
        return self.datum.assets.asset_a.assets + self.datum.assets.asset_b.assets


class WingRidersCPPState(AbstractConstantProductPoolState):
    """State for WingRiders constant product pool."""

    fee: int = 35
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1wxr2a8htmzuhj39y2gq7ftkpxv98y2g67tg8zezthgq4jkg0a4ul4",
    )

    @classmethod
    def dex(cls) -> str:
        """Returns the name of the DEX."""
        return "WingRiders"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Returns the order selector for the DEX."""
        return [cls._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector for the DEX."""
        return PoolSelector(
            selector_type="assets",
            selector=cls.dex_policy,
        )

    @property
    def swap_forward(self) -> bool:
        """Indicates if swap forwarding is supported."""
        return False

    @property
    def stake_address(self) -> Address:
        """Returns the stake address for the DEX."""
        return self._stake_address

    @classmethod
    def order_datum_class(cls) -> type[WingRidersOrderDatum]:
        """Returns the class for the order datum."""
        return WingRidersOrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[WingRidersPoolDatum]:
        """Returns the class for the pool datum."""
        return WingRidersPoolDatum

    @classmethod
    def pool_policy(cls) -> list[str]:
        """Returns the policy IDs for the pool."""
        return ["026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a570"]

    @classmethod
    def dex_policy(cls) -> list[str]:
        """Returns the policy IDs for the DEX."""
        return ["026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a5704c"]

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        if self.pool_nft is None:
            error_msg = "pool_nft is None"
            raise ValueError(error_msg)
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values: dict) -> bool:
        """Determines if initialization should be skipped based on the provided values."""
        if "pool_nft" in values and "dex_nft" in values:
            if cls.dex_policy()[0] not in values["dex_nft"]:
                error_msg = "Invalid DEX NFT"
                raise NotAPoolError(error_msg)
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
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Performs post-initialization tasks based on the provided values."""
        super().post_init(values)

        assets = values["assets"]
        datum = WingRidersPoolDatum.from_cbor(values["datum_cbor"])

        if len(assets) == ZERO_VALUE:
            assets.root[assets.unit(0)] -= 3000000

        assets.root[assets.unit(0)] -= datum.datum.quantity_a
        assets.root[assets.unit(1)] -= datum.datum.quantity_b
        return values

    def deposit(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
    ) -> Assets:
        """Calculates the deposit amount based on the given input and output assets."""
        merged_assets = (in_assets or Assets()) + (out_assets or Assets())
        if "lovelace" in merged_assets:
            return Assets(lovelace=4000000) - self.batcher_fee(
                in_assets=in_assets,
                out_assets=out_assets,
            )
        return self._deposit

    def batcher_fee(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
        extra_assets: Assets | None = None,  # noqa: ARG002
    ) -> Assets:
        """Calculates the batcher fee based on the given input and output assets."""
        merged_assets = (in_assets or Assets()) + (out_assets or Assets())
        if "lovelace" in merged_assets:
            if merged_assets["lovelace"] <= BATCHER_FEE_THRESHOLD_LOW:
                return Assets(lovelace=850000)
            if merged_assets["lovelace"] <= BATCHER_FEE_THRESHOLD_HIGH:
                return Assets(lovelace=1500000)
        return self._batcher


class WingRidersSSPState(AbstractStableSwapPoolState, WingRidersCPPState):
    """State for WingRiders stable swap pool."""

    fee: int = 6
    _batcher = Assets(lovelace=1500000)
    _deposit = Assets(lovelace=2000000)
    _stake_address = Address.from_primitive(
        "addr1w8z7qwzszt2lqy93m3atg2axx22yq5k7yvs9rmrvuwlawts2wzadz",
    )

    @classmethod
    def pool_policy(cls) -> list[str]:
        """Returns the policy IDs for the stable swap pool."""
        return ["980e8c567670d34d4ec13a0c3b6de6199f260ae5dc9dc9e867bc5c93"]

    @classmethod
    def dex_policy(cls) -> list[str]:
        """Returns the policy IDs for the DEX."""
        return ["980e8c567670d34d4ec13a0c3b6de6199f260ae5dc9dc9e867bc5c934c"]
