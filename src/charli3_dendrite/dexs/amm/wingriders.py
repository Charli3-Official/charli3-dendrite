"""WingRiders DEX Module."""

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from typing import ClassVar
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano.plutus import RawDatum

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_types import AbstractConstantProductPoolState
from charli3_dendrite.dexs.amm.amm_types import AbstractStableSwapPoolState
from charli3_dendrite.dexs.core.errors import NotAPoolError


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

    def order_type(self) -> OrderType | None:
        order_type = None
        if isinstance(self.detail, WingRidersOrderDetail):
            order_type = OrderType.swap
        elif isinstance(self.detail, WingRidersDepositDetail):
            order_type = OrderType.deposit
        elif isinstance(self.detail, WingRidersWithdrawDetail):
            order_type = OrderType.withdraw

        return order_type


@dataclass
class NoDatum(PlutusData):
    """WingRiders no datum."""

    CONSTR_ID = 0


@dataclass
class HashDatum(PlutusData):
    """WingRiders hash datum."""

    CONSTR_ID = 1


@dataclass
class InlineDatum(PlutusData):
    """WingRiders inline datum."""

    CONSTR_ID = 2


@dataclass
class SwapAction(PlutusData):
    """Swap."""

    CONSTR_ID = 0

    swap_direction: Union[AtoB, BtoA]
    minimum_receive: int


@dataclass
class AddLiquidityAction(PlutusData):
    """Add Liquidity."""

    CONSTR_ID = 1

    minimum_receive: int


@dataclass
class WithdrawLiquidityAction(PlutusData):
    """Add Liquidity."""

    CONSTR_ID = 2

    minimum_receive_a: int
    minimum_receive_b: int


@dataclass
class WithdrawProjectAction(PlutusData):
    """Unknown action."""

    CONSTR_ID = 3


@dataclass
class WithdrawProtocolAction(PlutusData):
    """Unknown action."""

    CONSTR_ID = 4


@dataclass
class WingRidersV2OrderDatum(OrderDatum):
    """WingRiders order datum."""

    oil: int
    beneficiary: PlutusFullAddress
    owner_address: PlutusFullAddress
    compensation_datum: Union[bytes, list, RawDatum]
    compensation_datum_type: Union[NoDatum, HashDatum, InlineDatum]
    deadline: int
    asset_a_symbol: bytes
    asset_a_token: bytes
    asset_b_symbol: bytes
    asset_b_token: bytes
    action: Union[
        SwapAction,
        AddLiquidityAction,
        WithdrawLiquidityAction,
        WithdrawProjectAction,
        WithdrawProtocolAction,
    ]
    a_scale: int
    b_scale: int

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
        a_scale: int = 1,
        b_scale: int = 1,
    ):
        """Create a WingRiders order datum."""
        timeout = int(((datetime.utcnow() + timedelta(days=360)).timestamp()) * 1000)

        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            direction = AtoB()
        else:
            direction = BtoA()

        plutus_address = PlutusFullAddress.from_address(address_source)

        return WingRidersV2OrderDatum(
            oil=2000000,
            beneficiary=plutus_address,
            owner_address=plutus_address,
            compensation_datum=b"",
            compensation_datum_type=NoDatum(),
            deadline=timeout,
            asset_a_symbol=(
                bytes.fromhex(merged.unit()[:56])
                if merged.unit() != "lovelace"
                else b""
            ),
            asset_a_token=(
                bytes.fromhex(merged.unit()[56:])
                if merged.unit() != "lovelace"
                else b""
            ),
            asset_b_symbol=bytes.fromhex(merged.unit(1)[:56]),
            asset_b_token=bytes.fromhex(merged.unit(1)[56:]),
            action=SwapAction(
                swap_direction=direction,
                minimum_receive=out_assets.quantity(),
            ),
            a_scale=a_scale,
            b_scale=b_scale,
        )

    def address_source(self) -> Address:
        return self.owner_address.to_address()

    def requested_amount(self) -> Assets:
        if isinstance(self.action, AddLiquidityAction):
            return Assets({"lp": self.action.minimum_receive})
        elif isinstance(self.action, SwapAction):
            if isinstance(self.action.swap_direction, BtoA):
                if self.asset_a_symbol.hex() == "":
                    asset_a = "lovelace"
                else:
                    asset_a = (self.asset_a_symbol + self.asset_a_token).hex()
                return Assets(
                    root={asset_a: self.action.minimum_receive},
                )
            else:
                asset_b = (self.asset_b_symbol + self.asset_b_token).hex()
                return Assets(
                    root={asset_b: self.action.minimum_receive},
                )
        elif isinstance(self.action, WithdrawLiquidityAction):
            return Assets(
                {
                    self.config.assets.asset_a.assets.unit(): self.action.min_amount_a,
                    self.config.assets.asset_b.assets.unit(): self.action.min_amount_b,
                },
            )

    def order_type(self) -> OrderType | None:
        order_type = None
        if isinstance(self.action, SwapAction):
            order_type = OrderType.swap
        elif isinstance(self.action, AddLiquidityAction):
            order_type = OrderType.deposit
        elif isinstance(self.action, WithdrawLiquidityAction):
            order_type = OrderType.withdraw

        return order_type


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


@dataclass
class CPPDatum(PlutusData):
    """WingRiders pool datum."""

    CONSTR_ID = 0


@dataclass
class SSPDatum(PlutusData):
    """WingRiders pool datum."""

    CONSTR_ID = 0

    parameter_d: int
    scale_a: int
    scale_b: int


@dataclass
class WingRidersV2PoolDatum(PoolDatum):
    """WingRiders pool datum."""

    CONSTR_ID = 0
    request_validator_hash: bytes
    asset_a_symbol: bytes
    asset_a_token: bytes
    asset_b_symbol: bytes
    asset_b_token: bytes
    swap_fee_in_basis: int
    protocol_fee_in_basis: int
    project_fee_in_basis: int
    reserve_fee_in_basis: int
    fee_basis: int
    agent_fee_ada: int
    last_interaction: int
    treasury_a: int
    treasury_b: int
    project_treasury_a: int
    project_treasury_b: int
    reserve_treasury_a: int
    reserve_treasury_b: int
    project_beneficiary: RawDatum
    reserve_beneficiary: RawDatum
    pool_specifics: CPPDatum

    def pool_pair(self) -> Assets | None:
        asset_a = (self.asset_a_symbol + self.asset_a_token).hex()
        if asset_a == "":
            asset_a = "lovelace"
        asset_b = (self.asset_b_symbol + self.asset_b_token).hex()
        return Assets(root={asset_a: 0, asset_b: 0})


@dataclass
class WingRidersV2SSPoolDatum(WingRidersV2PoolDatum):
    """WingRiders pool datum."""

    pool_specifics: SSPDatum


class WingRidersCPPState(AbstractConstantProductPoolState):
    """WingRiders CPP state."""

    fee: int = 35
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1wxr2a8htmzuhj39y2gq7ftkpxv98y2g67tg8zezthgq4jkg0a4ul4",
    )

    @classmethod
    def dex(cls) -> str:
        return "WingRiders"

    @classmethod
    def order_selector(self) -> list[str]:
        return [self._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=[
                "addr1w8nvjzjeydcn4atcd93aac8allvrpjn7pjr2qsweukpnayghhwcpj",
            ],
            assets=cls.dex_policy(),
        )

    @property
    def swap_forward(self) -> bool:
        return False

    @property
    def stake_address(self) -> Address:
        return self._stake_address

    @classmethod
    def order_datum_class(self) -> type[WingRidersOrderDatum]:
        return WingRidersOrderDatum

    @classmethod
    def pool_datum_class(self) -> type[WingRidersPoolDatum]:
        return WingRidersPoolDatum

    @classmethod
    def pool_policy(cls) -> str:
        return ["026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a570"]

    @classmethod
    def dex_policy(cls) -> str:
        return ["026a18d04a0c642759bb3d83b12e3344894e5c1c7b2aeb1a2113a5704c"]

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def skip_init(cls, values) -> bool:
        if "pool_nft" in values and "dex_nft" in values:
            if cls.dex_policy()[0] not in values["dex_nft"]:
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
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=[
                "addr1wxvx34v0hlxzk9x0clv7as9hvhn7dlzwj5xfcf6g4n5uucg4tkd7w",
            ],
            assets=cls.dex_policy(),
        )

    @classmethod
    def pool_policy(cls) -> str:
        return ["980e8c567670d34d4ec13a0c3b6de6199f260ae5dc9dc9e867bc5c93"]

    @classmethod
    def dex_policy(cls) -> str:
        return ["980e8c567670d34d4ec13a0c3b6de6199f260ae5dc9dc9e867bc5c934c"]


class WingRidersV2CPPState(AbstractConstantProductPoolState):
    """WingRiders CPP state."""

    fee: int = 35
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1w8qnfkpe5e99m7umz4vxnmelxs5qw5dxytmfjk964rla98q605wte",
    )
    _pool_datum_parsed: WingRidersV2PoolDatum | None = None

    @classmethod
    def dex(cls) -> str:
        return "WingRidersV2"

    @classmethod
    def order_selector(self) -> list[str]:
        return [self._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1wxhew7fmsup08qvhdnkg8ccra88pw7q5trrncja3dlszhqczc0qfe"],
            assets=cls.dex_policy(),
        )

    @property
    def swap_forward(self) -> bool:
        return False

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @property
    def stake_address(self) -> Address:
        return self._stake_address

    @classmethod
    def order_datum_class(cls) -> type[WingRidersV2OrderDatum]:
        return WingRidersV2OrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[WingRidersV2PoolDatum]:
        return WingRidersV2PoolDatum

    @classmethod
    def pool_policy(cls) -> list[str]:
        return ["6fdc63a1d71dc2c65502b79baae7fb543185702b12c3c5fb639ed737"]

    @classmethod
    def dex_policy(cls) -> list[str]:
        return ["6fdc63a1d71dc2c65502b79baae7fb543185702b12c3c5fb639ed7374c"]

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns default script class of type PlutusV1Script or PlutusV2Script."""
        return PlutusV2Script

    @classmethod
    def skip_init(cls, values) -> bool:
        if "pool_nft" in values and "dex_nft" in values:
            if cls.dex_policy()[0] not in values["dex_nft"]:
                raise NotAPoolError("Invalid DEX NFT")

            if not isinstance(values["assets"], Assets):
                values["assets"] = Assets.model_validate(values["assets"])

            cls._post_init(values)

            return True
        else:
            return False

    @property
    def pool_datum(self) -> PlutusData:
        """The pool state datum."""
        if self._pool_datum_parsed is None:
            self._pool_datum_parsed = self.pool_datum_class().from_cbor(self.datum_cbor)
        return self._pool_datum_parsed

    @classmethod
    def _post_init(cls, values):
        super().post_init(values)

        try:
            datum = WingRidersV2SSPoolDatum.from_cbor(values["datum_cbor"])
            if not issubclass(cls, WingRidersV2SSPState):
                raise NotAPoolError("Invalid Datum")
        except TypeError:
            if issubclass(cls, WingRidersV2SSPState):
                raise NotAPoolError("Invalid Datum")
            datum = WingRidersV2PoolDatum.from_cbor(values["datum_cbor"])

        values["fee"] = int(
            (
                datum.swap_fee_in_basis
                + datum.protocol_fee_in_basis
                + datum.project_fee_in_basis
            )
            * 10000
            / datum.fee_basis,
        )

        return datum

    @classmethod
    def post_init(cls, values):
        datum = cls._post_init(values)

        assets = values["assets"]

        if len(assets) == 2:
            assets.root[assets.unit(0)] -= 3000000

        assets.root[assets.unit(0)] -= (
            datum.treasury_a + datum.project_treasury_a + datum.reserve_treasury_a
        )
        assets.root[assets.unit(1)] -= (
            datum.treasury_b + datum.project_treasury_b + datum.reserve_treasury_b
        )

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
        return Assets(lovelace=2000000)


class WingRidersV2SSPState(AbstractStableSwapPoolState, WingRidersV2CPPState):
    """WingRiders SSP state."""

    fee: int = 6
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)
    _stake_address = Address.from_primitive(
        "addr1wy3ksr4xwqd4dukp9tnemzheflfk75ym0vq8q2w8ecg5ssqmfdjaz",
    )
    _pool_datum_parsed: WingRidersV2PoolDatum | None = None

    asset_mulitipliers: ClassVar[list[int]] = [1, 1]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1wx2x4c3ggv8jl3j24ze6ewgsacn7nvly0250jf06cfurfggd7zqtl"],
            assets=cls.dex_policy(),
        )

    @classmethod
    def pool_datum_class(cls) -> type[WingRidersV2SSPoolDatum]:
        return WingRidersV2SSPoolDatum

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        datum: WingRidersV2SSPoolDatum = WingRidersV2SSPoolDatum.from_cbor(
            values["datum_cbor"],
        )

        cls.asset_mulitipliers = [
            datum.pool_specifics.scale_a,
            datum.pool_specifics.scale_b,
        ]

    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> PlutusData:
        return self.order_datum_class().create_datum(
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            batcher_fee=self.batcher_fee(
                in_assets=in_assets,
                out_assets=out_assets,
                extra_assets=extra_assets,
            ),
            deposit=self.deposit(in_assets=in_assets, out_assets=out_assets),
            address_target=address_target,
            datum_target=datum_target,
            a_scale=self.asset_mulitipliers[0],
            b_scale=self.asset_mulitipliers[1],
        )
