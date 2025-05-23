"""Abstract base class and common functions for handling token pairs."""

from abc import ABC
from abc import abstractmethod
from decimal import Decimal

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import UTxO

from charli3_dendrite.dataclasses.datums import CancelRedeemer
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolSelector


class AbstractPairState(DendriteBaseModel, ABC):
    """Abstract base class representing the state of a pair."""

    assets: Assets
    block_time: int
    block_index: int
    fee: int | list[int] | None = None
    plutus_v2: bool
    tx_index: int | None = None
    tx_hash: str | None = None
    datum_cbor: str | None = None
    datum_hash: str | None = None
    dex_nft: Assets | None = None

    _batcher_fee: Assets
    _datum_parsed: PlutusData

    @classmethod
    @abstractmethod
    def dex(cls) -> str:
        """Official dex name."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        raise NotImplementedError

    @abstractmethod
    def get_amount_out(self, asset: Assets) -> tuple[Assets, float]:
        """Calculate the output amount of assets for given input.

        Args:
            asset: An asset with a defined quantity.

        Returns:
            A tuple where the first value is the estimated asset returned and
            the second value is the price impact ratio.
        """
        raise NotImplementedError

    @abstractmethod
    def get_amount_in(self, asset: Assets) -> tuple[Assets, float]:
        """Get the input asset amount given a desired output asset amount.

        Args:
            asset: An asset with a defined quantity.

        Returns:
           A tuple where the first value is the the estimated asset needed and
           the second value is the slippage fee.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def swap_forward(self) -> bool:
        """Returns if swap forwarding is enabled."""
        raise NotImplementedError

    @property
    def inline_datum(self) -> bool:
        """Determine whether the datum should be inline."""
        return self.plutus_v2

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Get Reference UTXO.

        Returns:
            UTxO | None: UTxO object if it exists, otherwise None.
        """
        return None

    @property
    @abstractmethod
    def stake_address(self) -> Address:
        """Return the staking address."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns data class used for handling order datums."""
        raise NotImplementedError

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Get default script class as Plutus V1 unless overridden.

        Returns:
            type[PlutusV1Script] | type[PlutusV2Script]: The default script class.
        """
        return PlutusV1Script

    def script_class(self) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the script class based on the Plutus version being used."""
        if self.plutus_v2:
            return PlutusV2Script
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
        """Constructs the datum for a swap transaction."""
        raise NotImplementedError

    @abstractmethod
    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder | None = None,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Constructs the transaction output for a swap."""
        raise NotImplementedError

    @property
    def volume_fee(self) -> int | float | list[int] | list[float] | None:
        """Swap fee of swap in basis points."""
        return self.fee

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        """Returns the redeemer data for canceling transaction."""
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
