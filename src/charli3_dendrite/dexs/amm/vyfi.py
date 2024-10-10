"""VyFi DEX Module."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import Optional
from typing import Union

import requests
from pycardano import Address
from pycardano import PlutusData
from pycardano import VerificationKeyHash
from pydantic import BaseModel
from pydantic import Field

from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_types import AbstractConstantProductPoolState
from charli3_dendrite.dexs.core.errors import NoAssetsError
from charli3_dendrite.dexs.core.errors import NotAPoolError
from charli3_dendrite.utility import Assets

POOL_REFRESH_INTERVAL = 3600
ADDRESS_HASH_LENGTH = 28
POLICY_ID_LENGTH = 56


@dataclass
class VyFiPoolDatum(PoolDatum):
    """VyFi pool datum."""

    token_a_fees: int
    token_b_fees: int
    lp_tokens: int

    def pool_pair(self) -> Optional[Assets]:
        """Return the pool pair assets."""
        return None


@dataclass
class Deposit(PlutusData):
    """Deposit assets into the pool."""

    CONSTR_ID = 0
    min_lp_receive: int


@dataclass
class WithdrawPair(PlutusData):
    """Withdraw pair of assets."""

    CONSTR_ID = 0
    min_amount_a: int
    min_amount_b: int


@dataclass
class Withdraw(PlutusData):
    """Withdraw assets from the pool."""

    CONSTR_ID = 1
    min_lp_receive: WithdrawPair


@dataclass
class LPFlushA(PlutusData):
    """Flush LP tokens from A."""

    CONSTR_ID = 2


@dataclass
class AtoB(PlutusData):
    """A to B swap direction."""

    CONSTR_ID = 3
    min_receive: int


@dataclass
class BtoA(PlutusData):
    """B to A swap direction."""

    CONSTR_ID = 4
    min_receive: int


@dataclass
class ZapInA(PlutusData):
    """Zap in A."""

    CONSTR_ID = 5
    min_lp_receive: int


@dataclass
class ZapInB(PlutusData):
    """Zap in B."""

    CONSTR_ID = 6
    min_lp_receive: int


@dataclass
class VyFiOrderDatum(OrderDatum):
    """VyFi order datum."""

    address: bytes
    order: Union[AtoB, BtoA, Deposit, LPFlushA, Withdraw, ZapInA, ZapInB]

    @classmethod
    def create_datum(
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,  # noqa: ARG003
        deposit: Assets,  # noqa: ARG003
        address_target: Optional[Address] = None,  # noqa: ARG003
        datum_target: Optional[PlutusData] = None,  # noqa: ARG003
    ) -> VyFiOrderDatum:
        """Create a new order datum."""
        address_hash = (
            address_source.payment_part.to_primitive()
            + address_source.staking_part.to_primitive()
        )

        merged = in_assets + out_assets
        if in_assets.unit() == merged.unit():
            order = AtoB(min_receive=out_assets.quantity())
        else:
            order = BtoA(min_receive=out_assets.quantity())

        return cls(address=address_hash, order=order)

    def address_source(self) -> Address:
        """Get the source address."""
        payment_part = VerificationKeyHash.from_primitive(
            self.address[:ADDRESS_HASH_LENGTH],
        )
        staking_part = (
            VerificationKeyHash.from_primitive(self.address[ADDRESS_HASH_LENGTH:])
            if len(self.address) > ADDRESS_HASH_LENGTH
            else None
        )
        return Address(payment_part=payment_part, staking_part=staking_part)

    def requested_amount(self) -> Assets:
        """Get the requested amount."""
        if isinstance(self.order, BtoA):
            return Assets({"asset_a": self.order.min_receive})
        if isinstance(self.order, AtoB):
            return Assets({"asset_b": self.order.min_receive})
        if isinstance(self.order, (ZapInA, ZapInB, Deposit)):
            return Assets({"lp": self.order.min_lp_receive})
        if isinstance(self.order, Withdraw):
            return Assets(
                {
                    "asset_a": self.order.min_lp_receive.min_amount_a,
                    "asset_b": self.order.min_lp_receive.min_amount_b,
                },
            )
        return Assets()

    def order_type(self) -> Optional[OrderType]:
        """Get the order type."""
        if isinstance(self.order, (BtoA, AtoB, ZapInA, ZapInB)):
            return OrderType.swap
        if isinstance(self.order, Deposit):
            return OrderType.deposit
        if isinstance(self.order, Withdraw):
            return OrderType.withdraw
        return None


class VyFiTokenDefinition(BaseModel):
    """VyFi token definition."""

    token_name: str = Field(alias="tokenName")
    currency_symbol: str = Field(alias="currencySymbol")


class VyFiFees(BaseModel):
    """VyFi fees."""

    bar_fee: int = Field(alias="barFee")
    process_fee: int = Field(alias="processFee")
    liq_fee: int = Field(alias="liqFee")


class VyFiPoolTokens(BaseModel):
    """VyFi pool tokens."""

    a_asset: VyFiTokenDefinition = Field(alias="aAsset")
    b_asset: VyFiTokenDefinition = Field(alias="bAsset")
    main_nft: VyFiTokenDefinition = Field(alias="mainNFT")
    operator_token: VyFiTokenDefinition = Field(alias="operatorToken")
    lp_token_name: dict[str, str] = Field(alias="lpTokenName")
    fees_settings: VyFiFees = Field(alias="feesSettings")
    stake_key: Optional[str] = Field(alias="stakeKey")


class VyFiPoolDefinition(BaseModel):
    """VyFi pool definition."""

    units_pair: str = Field(alias="unitsPair")
    pool_validator_utxo_address: str = Field(alias="poolValidatorUtxoAddress")
    lp_policy_id_asset_id: str = Field(alias="lpPolicyId-assetId")
    json_: VyFiPoolTokens = Field(alias="json")
    pair: str
    is_live: bool = Field(alias="isLive")
    order_validator_utxo_address: str = Field(alias="orderValidatorUtxoAddress")

    def __hash__(self) -> int:
        """Make VyFiPoolDefinition hashable."""
        return hash(
            (
                self.units_pair,
                self.pool_validator_utxo_address,
                self.order_validator_utxo_address,
            ),
        )


class VyFiCPPState(AbstractConstantProductPoolState):
    """VyFi CPP state."""

    _batcher = Assets(lovelace=1900000)
    _deposit = Assets(lovelace=2000000)
    _pools: ClassVar[Optional[dict[str, VyFiPoolDefinition]]] = None
    _pools_refresh: ClassVar[float] = 0.0
    lp_fee: int = 0
    bar_fee: int = 0

    @classmethod
    def dex(cls) -> str:
        """Get the DEX name."""
        return "VyFi"

    @classmethod
    def pools(cls) -> dict[str, VyFiPoolDefinition]:
        """Get the pools."""
        if (
            cls._pools is None
            or (time.time() - cls._pools_refresh) > POOL_REFRESH_INTERVAL
        ):
            cls._refresh_pools()
        return cls._pools or {}

    @classmethod
    def order_selector(cls) -> list[str]:
        """Get order selector addresses."""
        return [p.order_validator_utxo_address for p in cls.pools().values()]

    @classmethod
    def pool_selector(cls, assets: Optional[list[str]] = None) -> PoolSelector:
        """Get a PoolSelector for VyFi pools, optionally filtered by assets."""
        asset_to_pool = cls._create_asset_to_pool_mapping()
        relevant_pools = cls._filter_relevant_pools(asset_to_pool, assets)
        addresses = [pool.pool_validator_utxo_address for pool in relevant_pools]
        return PoolSelector(addresses=addresses)

    @classmethod
    def _create_asset_to_pool_mapping(
        cls,
    ) -> defaultdict[str, list[VyFiPoolDefinition]]:
        """Create a mapping of assets to pools."""
        asset_to_pool: defaultdict[str, list[VyFiPoolDefinition]] = defaultdict(list)
        for pool in cls.pools().values():
            asset_a = cls._encode_asset(
                pool.json_.a_asset.currency_symbol,
                pool.json_.a_asset.token_name,
            )
            asset_b = cls._encode_asset(
                pool.json_.b_asset.currency_symbol,
                pool.json_.b_asset.token_name,
            )
            asset_to_pool[asset_a].append(pool)
            asset_to_pool[asset_b].append(pool)
        return asset_to_pool

    @classmethod
    def _filter_relevant_pools(
        cls,
        asset_to_pool: defaultdict[str, list[VyFiPoolDefinition]],
        assets: Optional[list[str]],
    ) -> set[VyFiPoolDefinition]:
        """Filter relevant pools based on assets."""
        if assets:
            relevant_pools = set()
            for asset in assets:
                relevant_pools.update(asset_to_pool.get(asset, []))
        else:
            relevant_pools = set(cls.pools().values())
        return relevant_pools

    @staticmethod
    def _encode_asset(policy_id: str, asset_name: str) -> str:
        """Encode an asset by combining policy ID and hex-encoded asset name."""
        encoded_name = asset_name.encode("utf-8").hex()
        return policy_id + encoded_name

    @staticmethod
    def _decode_asset(encoded_asset: str) -> tuple[str, str]:
        """Decode an encoded asset into policy ID and asset name."""
        policy_id = encoded_asset[:POLICY_ID_LENGTH]
        asset_name = bytes.fromhex(encoded_asset[POLICY_ID_LENGTH:]).decode("utf-8")
        return policy_id, asset_name

    @staticmethod
    def _split_asset(asset: str) -> tuple[str, str]:
        """Split an asset string into policy ID and asset name."""
        if len(asset) == POLICY_ID_LENGTH:  # Only policy ID
            return asset, ""
        return asset[:POLICY_ID_LENGTH], asset[POLICY_ID_LENGTH:]

    @classmethod
    def _refresh_pools(cls) -> None:
        """Refresh the pools data from the API."""
        try:
            response = requests.get(
                "https://api.vyfi.io/lp?networkId=1&v2=true",
                timeout=10,
            )
            response.raise_for_status()
            cls._pools = {}
            for p in response.json():
                p["json"] = json.loads(p["json"])
                cls._pools[
                    p["json"]["mainNFT"]["currencySymbol"]
                ] = VyFiPoolDefinition.model_validate(p)
            cls._pools_refresh = time.time()
        except requests.RequestException as e:
            # Log the error or handle it as appropriate for your application
            print(f"Error refreshing pools: {e}")  # noqa: T201

    @property
    def swap_forward(self) -> bool:
        """Check if swap is forward."""
        return False

    @property
    def stake_address(self) -> Address:
        """Get the stake address."""
        return Address.from_primitive(
            VyFiCPPState.pools()[self.pool_id].order_validator_utxo_address,
        )

    @classmethod
    def order_datum_class(cls) -> type[VyFiOrderDatum]:
        """Get the order datum class."""
        return VyFiOrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[VyFiPoolDatum]:
        """Get the pool datum class."""
        return VyFiPoolDatum

    @property
    def pool_id(self) -> str:
        """Get a unique identifier for the pool."""
        return self.pool_nft.unit()

    @property
    def volume_fee(self) -> int:
        """Get the volume fee."""
        return self.lp_fee + self.bar_fee

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Optional[Assets]:
        """Extract the dex nft from the UTXO."""
        assets = values["assets"]

        if "pool_nft" in values:
            pool_nft = (
                Assets(root=values["pool_nft"])
                if isinstance(values["pool_nft"], dict)
                else values["pool_nft"]
            )
            if not any(p in cls.pools() for p in values["pool_nft"]):
                raise ValueError("Invalid pool NFT")
        else:
            nfts = [asset for asset, quantity in assets.items() if asset in cls.pools()]
            if len(nfts) < 1:
                if len(assets) == 0:
                    raise NoAssetsError(f"{cls.__name__}: No assets supplied.")
                raise NotAPoolError(
                    f"{cls.__name__}: Pool must have one DEX NFT token.",
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        values["lp_fee"] = cls.pools()[pool_nft.unit()].json_.fees_settings.liq_fee
        values["bar_fee"] = cls.pools()[pool_nft.unit()].json_.fees_settings.bar_fee

        return pool_nft

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> None:
        """Post-initialization processing."""
        super().post_init(values)

        assets = values["assets"]
        datum = VyFiPoolDatum.from_cbor(values["datum_cbor"])

        assets.root[assets.unit(0)] -= datum.token_a_fees
        assets.root[assets.unit(1)] -= datum.token_b_fees
