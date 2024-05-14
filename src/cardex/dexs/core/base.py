from abc import ABC
from abc import abstractmethod
from decimal import Decimal

from cardex.dataclasses.datums import CancelRedeemer
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import CardexBaseModel
from cardex.dataclasses.models import PoolSelector
from cardex.utility import Assets
from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionOutput
from pycardano import UTxO


class AbstractPairState(CardexBaseModel, ABC):
    assets: Assets
    block_time: int
    block_index: int
    fee: int | None = None
    plutus_v2: bool
    tx_index: int | None = None
    tx_hash: str | None = None
    datum_cbor: str | None = None
    datum_hash: str | None = None
    dex_nft: Assets | None = None

    _batcher_fee: Assets
    _datum_parsed: PlutusData
    # _deposit: Assets

    @classmethod
    @abstractmethod
    def dex(self) -> str:
        """Official dex name."""
        raise NotImplementedError("DEX name is undefined.")

    @classmethod
    @abstractmethod
    def order_selector(self) -> list[str]:
        """Order selection information."""
        raise NotImplementedError("DEX name is undefined.")

    @classmethod
    @abstractmethod
    def pool_selector(self) -> PoolSelector:
        """Pool selection information."""
        raise NotImplementedError("DEX name is undefined.")

    @abstractmethod
    def get_amount_out(self, asset: Assets) -> tuple[Assets, float]:
        raise NotImplementedError("")

    @abstractmethod
    def get_amount_in(self, asset: Assets) -> tuple[Assets, float]:
        raise NotImplementedError("")

    @property
    @abstractmethod
    def swap_forward(self) -> bool:
        raise NotImplementedError

    @property
    def inline_datum(self) -> bool:
        return self.plutus_v2

    @classmethod
    @property
    def reference_utxo(self) -> UTxO | None:
        return None

    @property
    @abstractmethod
    def stake_address(self) -> Address:
        raise NotImplementedError

    @property
    @abstractmethod
    def order_datum_class(self) -> type[PlutusData]:
        raise NotImplementedError

    @classmethod
    def default_script_class(self) -> type[PlutusV1Script] | type[PlutusV2Script]:
        return PlutusV1Script

    @property
    def script_class(self) -> type[PlutusV1Script] | type[PlutusV2Script]:
        if self.plutus_v2:
            return PlutusV2Script
        else:
            return PlutusV1Script

    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> PlutusData:
        raise NotImplementedError

    @abstractmethod
    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> TransactionOutput:
        raise NotImplementedError

    @property
    def volume_fee(self) -> int:
        """Swap fee of swap in basis points."""
        return self.fee

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        return Redeemer(CancelRedeemer())

    def batcher_fee(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
        extra_assets: Assets | None = None,
    ) -> Assets:
        """Batcher fee.

        Args:
            in_assets: The input assets for the swap
            out_assets: The output assets for the swap
            extra_assets: Extra assets included in the transaction
        """
        return self._batcher

    def deposit(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
    ) -> Assets:
        """Batcher fee."""
        return self._deposit

    @classmethod
    @property
    def dex_policy(cls) -> list[str] | None:
        """The dex nft policy.

        This should be the policy or policy+name of the dex nft.

        If None, then the default dex nft check is skipped.

        Returns:
            Optional[str]: policy or policy+name of dex nft
        """
        return None

    @property
    def unit_a(self) -> str:
        """Token name of asset A."""
        return self.assets.unit(0)

    @property
    def unit_b(self) -> str:
        """Token name of asset b."""
        return self.assets.unit(1)

    @property
    def reserve_a(self) -> int:
        """Reserve amount of asset A."""
        return self.assets.quantity(0)

    @property
    def reserve_b(self) -> int:
        """Reserve amount of asset B."""
        return self.assets.quantity(1)

    @property
    @abstractmethod
    def price(self) -> tuple[Decimal, Decimal]:
        """Price of assets.

        Returns:
            A `Tuple[Decimal, Decimal] where the first `Decimal` is the price to buy
                1 of token B in units of token A, and the second `Decimal` is the price
                to buy 1 of token A in units of token B.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def tvl(self) -> Decimal:
        """Return the total value locked for the pool.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        raise NotImplementedError
