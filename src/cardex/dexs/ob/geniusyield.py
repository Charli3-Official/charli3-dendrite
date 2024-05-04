from dataclasses import dataclass
from typing import Union

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import PoolSelector
from cardex.dataclasses.models import PoolSelectorType
from cardex.dexs.ob.ob_base import AbstractOrderState
from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import TransactionOutput


@dataclass
class GeniusContainedFee(PlutusData):
    CONSTR_ID = 0
    lovelaces: int
    offered_tokens: int
    asked_tokens: int


@dataclass
class GeniusTimestamp(PlutusData):
    CONSTR_ID = 0
    timestamp: int


@dataclass
class GeniusNullTimestamp(PlutusData):
    CONSTR_ID = 0
    timestamp: int


@dataclass
class GeniusRational(PlutusData):
    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class GeniusYieldOrder(PlutusData):
    CONSTR_ID = 0
    owner_key: bytes
    owner_address: PlutusFullAddress
    offered_asset: AssetClass
    offered_original_amount: int
    offered_amount: int
    asked_asset: AssetClass
    price: GeniusRational
    nft: bytes
    start_time: Union[GeniusTimestamp, PlutusNone]
    end_time: Union[GeniusTimestamp, PlutusNone]
    partial_fills: int
    maker_lovelace_fee: int
    taker_lovelace_fee: int
    contained_fee: GeniusContainedFee
    contained_payment: int

    def pool_pair(self) -> Assets | None:
        return self.offered_asset.assets + self.asked_asset.assets


class GeniusYield(AbstractOrderState):
    """This class is largely used for OB dexes that allow direct script inputs."""

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False

    _batcher_fee: Assets
    _datum_parsed: PlutusData

    @classmethod
    @property
    def dex_policy(cls) -> list[str] | None:
        """The dex nft policy.

        This should be the policy or policy+name of the dex nft.

        If None, then the default dex nft check is skipped.

        Returns:
            Optional[str]: policy or policy+name of dex nft
        """
        return [
            "22f6999d4effc0ade05f6e1a70b702c65d6b3cdf0e301e4a8267f585",
            "642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503",
        ]

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "GeniusYield"

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> TransactionOutput:
        return None

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return [
            "addr1wx5d0l6u7nq3wfcz3qmjlxkgu889kav2u9d8s5wyzes6frqktgru2",
            "addr1w8kllanr6dlut7t480zzytsd52l7pz4y3kcgxlfvx2ddavcshakwd",
        ]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        return PoolSelector(
            selector_type=PoolSelectorType.address,
            selector=cls.order_selector(),
        )

    @property
    def swap_forward(self) -> bool:
        return True

    @property
    def stake_address(self) -> Address | None:
        return None

    @classmethod
    @property
    def order_datum_class(self) -> type[PlutusData]:
        return GeniusYieldOrder

    @classmethod
    def default_script_class(self) -> type[PlutusV1Script] | type[PlutusV2Script]:
        return PlutusV2Script

    @property
    def price(self) -> tuple[int, int]:
        return [self.order_datum.price[0], self.order_datum.price[1]]

    @property
    def available(self) -> Assets:
        """Max amount of output asset that can be used to fill the order."""
        return (
            self.order_datum.offered_original_amount - self.order_datum.offered_amount
        )

    @property
    def tvl(self) -> int:
        """Return the total value locked in the order

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        return self.available

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        return self.dex_nft
