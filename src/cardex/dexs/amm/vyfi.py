"""Data classes and utilities for Vyfi Dex.

This contains data classes and utilities for handling various order and pool datums
"""
import json
import time
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

from cardex.dataclasses.datums import OrderDatum
from cardex.dataclasses.datums import PoolDatum
from cardex.dataclasses.models import OrderType
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.amm.amm_types import AbstractConstantProductPoolState
from cardex.dexs.core.constants import ADDRESS_LENGTH
from cardex.dexs.core.constants import ONE_VALUE
from cardex.dexs.core.constants import POOLS_REFRESH_INTERVAL_SECONDS
from cardex.dexs.core.constants import ZERO_VALUE
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError
from cardex.utility import Assets


@dataclass
class VyFiPoolDatum(PoolDatum):
    """Represents the datum for a VyFi liquidity pool.

    TODO: Figure out what each of these numbers mean.
    """

    a: int
    b: int
    c: int

    def pool_pair(self) -> Assets | None:
        """Returns the pair of assets in the liquidity pool."""
        return None


@dataclass
class Deposit(PlutusData):
    """Represents a deposit in the VyFi pool."""

    CONSTR_ID = 0
    min_lp_receive: int


@dataclass
class WithdrawPair(PlutusData):
    """Represents a pair of assets to withdraw from the VyFi pool."""

    CONSTR_ID = 0
    min_amount_a: int
    min_amount_b: int


@dataclass
class Withdraw(PlutusData):
    """Represents a withdrawal in the VyFi pool."""

    CONSTR_ID = 1
    min_lp_receive: WithdrawPair


@dataclass
class LPFlushA(PlutusData):
    """Represents a liquidity pool flush operation."""

    CONSTR_ID = 2


@dataclass
class AtoB(PlutusData):
    """Represents an asset swap from asset A to asset B."""

    CONSTR_ID = 3
    min_receive: int


@dataclass
class BtoA(PlutusData):
    """Represents an asset swap from asset B to asset A."""

    CONSTR_ID = 4
    min_receive: int


@dataclass
class ZapInA(PlutusData):
    """Represents a zap-in operation for asset A."""

    CONSTR_ID = 5
    min_lp_receive: int


@dataclass
class ZapInB(PlutusData):
    """Represents a zap-in operation for asset B."""

    CONSTR_ID = 6
    min_lp_receive: int


@dataclass
class VyFiOrderDatum(OrderDatum):
    """Represents the order datum for VyFi."""

    CONSTR_ID = 0
    address: bytes
    order: Union[AtoB, BtoA, Deposit, LPFlushA, Withdraw, ZapInA, ZapInB]

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
    ) -> "VyFiOrderDatum":
        """Creates a VyFiOrderDatum instance.

        Args:
            address_source: The source address.
            in_assets: Input assets.
            out_assets: Output assets.
            batcher_fee: Fee for the batcher.
            deposit: Deposit amount.
            address_target: Target address (optional).
            datum_target: Target datum (optional).

        Returns:
            A VyFiOrderDatum instance.
        """
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
        """Returns the source address of the order."""
        payment_part = VerificationKeyHash.from_primitive(self.address[:ADDRESS_LENGTH])
        if len(self.address) == ADDRESS_LENGTH:
            staking_part = None
        else:
            staking_part = VerificationKeyHash.from_primitive(self.address[28:56])
        return Address(payment_part=payment_part, staking_part=staking_part)

    def requested_amount(self) -> Assets:
        """Returns the requested amount for the order."""
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
        error_msg = "Invalid detail type for requested_amount"
        raise ValueError(error_msg)

    def order_type(self) -> OrderType:
        """Returns the type of the order."""
        if isinstance(self.order, (BtoA, AtoB)):
            return OrderType.swap
        if isinstance(self.order, Deposit):
            return OrderType.deposit
        if isinstance(self.order, Withdraw):
            return OrderType.withdraw
        if isinstance(self.order, (ZapInA, ZapInB)):
            return OrderType.zap_in
        error_msg = "Invalid detail type for order_type"
        raise ValueError(error_msg)


class VyFiTokenDefinition(BaseModel):
    """Represents the definition of a VyFi token."""

    token_name: str
    currency_symbol: str


class VyFiFees(BaseModel):
    """Represents the fees in the VyFi protocol."""

    bar_fee: int
    process_fee: int
    liq_fee: int


class VyFiPoolTokens(BaseModel):
    """Represents the tokens in a VyFi liquidity pool."""

    a_asset: VyFiTokenDefinition
    b_asset: VyFiTokenDefinition
    main_nft: VyFiTokenDefinition
    operator_token: VyFiTokenDefinition
    lptoken_name: dict[str, str]
    fees_settings: VyFiFees
    stake_key: Optional[str]


class VyFiPoolDefinition(BaseModel):
    """Represents the definition of a VyFi liquidity pool."""

    units_pair: str
    pool_validator_utxo_address: str
    lp_policy_id_asset_id: str = Field(alias="lpPolicyId-assetId")
    json_: VyFiPoolTokens = Field(alias="json")
    pair: str
    is_live: bool
    order_validator_utxo_address: str


class VyFiCPPState(AbstractConstantProductPoolState):
    """Represents the state for VyFi constant product pool."""

    _batcher = Assets(lovelace=1900000)
    _deposit = Assets(lovelace=2000000)
    _pools: ClassVar[dict[str, VyFiPoolDefinition] | None] = None
    _pools_refresh: ClassVar[float] = time.time()
    lp_fee: int
    bar_fee: int

    @classmethod
    def dex(cls) -> str:
        """Returns the name of the DEX."""
        return "VyFi"

    @classmethod
    def pools(cls) -> dict[str, VyFiPoolDefinition]:
        """Returns the pools in the DEX."""
        if (
            cls._pools is None
            or (time.time() - cls._pools_refresh) > POOLS_REFRESH_INTERVAL_SECONDS
        ):
            cls._pools = {}
            for p in requests.get(
                "https://api.vyfi.io/lp?networkId=1&v2=true",
                timeout=10,
            ).json():
                p["json"] = json.loads(p["json"])
                cls._pools[
                    p["json"]["main_nft"]["currency_symbol"]
                ] = VyFiPoolDefinition.model_validate(p)
            cls._pools_refresh = time.time()

        return cls._pools

    @classmethod
    def order_selector(cls) -> list[str]:
        """Returns the order selector for the DEX."""
        if cls._pools is None:
            return []
        return [p.order_validator_utxo_address for p in cls._pools.values()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector for the DEX."""
        if cls._pools is None:
            return PoolSelector(selector_type="addresses", selector=[])
        return PoolSelector(
            selector_type="addresses",
            selector=[pool.pool_validator_utxo_address for pool in cls._pools.values()],
        )

    @property
    def swap_forward(self) -> bool:
        """Indicates if swap forwarding is supported."""
        return False

    @property
    def stake_address(self) -> Address:
        """Returns the stake address for the DEX."""
        if VyFiCPPState._pools is None:
            error_msg = "Pools data is not available."
            raise ValueError(error_msg)
        return Address.from_primitive(
            VyFiCPPState._pools[self.pool_id].order_validator_utxo_address,
        )

    @classmethod
    def order_datum_class(cls) -> type[VyFiOrderDatum]:
        """Returns the class for the order datum."""
        return VyFiOrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[VyFiPoolDatum]:
        """Returns the class for the pool datum."""
        return VyFiPoolDatum

    @property
    def pool_id(self) -> str:
        """Returns a unique identifier for the pool."""
        if self.pool_nft is None:
            error_msg = "pool_nft is None"
            raise ValueError(error_msg)
        return self.pool_nft.unit()

    @property
    def volume_fee(self) -> int:
        """Returns the volume fee for the pool."""
        return self.lp_fee + self.bar_fee

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

        # If the dex nft is in the values, it's been parsed already
        if "pool_nft" in values:
            if cls._pools is None or not any(
                p in cls._pools for p in values["pool_nft"]
            ):
                error_msg = "None of the specified NFT pools are valid."
                raise ValueError(error_msg)
            if isinstance(values["pool_nft"], dict):
                pool_nft = Assets(root=values["pool_nft"])
            else:
                pool_nft = values["pool_nft"]

        # Check for the dex nft
        else:
            nfts = [
                asset
                for asset, quantity in assets.items()
                if cls._pools is not None and asset in cls._pools
            ]
            if len(nfts) < ONE_VALUE:
                if len(assets) == ZERO_VALUE:
                    error_msg = f"{cls.__name__}: No assets supplied."
                    raise NoAssetsError(
                        error_msg,
                    )
                error_msg = f"{cls.__name__}: Pool must have one DEX NFT token."
                raise NotAPoolError(
                    error_msg,
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft
        if cls._pools:
            values["lp_fee"] = cls._pools[pool_nft.unit()].json_.fees_settings.liq_fee
            values["bar_fee"] = cls._pools[pool_nft.unit()].json_.fees_settings.bar_fee
        else:
            error_msg = "Pools data is not available."
            raise ValueError(error_msg)
        return pool_nft
