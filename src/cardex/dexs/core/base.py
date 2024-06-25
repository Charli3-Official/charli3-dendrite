"""This module defines the abstract base class for a trading pair."""
from abc import ABC
from abc import abstractmethod
from decimal import Decimal

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionOutput
from pycardano import UTxO

from cardex.dataclasses.datums import CancelRedeemer
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import CardexBaseModel
from cardex.dataclasses.models import PoolSelector


class AbstractPairState(CardexBaseModel, ABC):
    """Abstract base class representing the state of a trading pair in a DEX.

    Attributes:
        assets (Assets): The assets in the trading pair.
        block_time (int): The time of the block.
        block_index (int): The index of the block.
        fee (float | None): The fee for the transaction.
        plutus_v2 (bool): Indicates if Plutus V2 is used.
        _batcher_fee (Assets): The batcher fee.
        _datum_parsed (PlutusData): The parsed datum.
    """

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

    @classmethod
    @abstractmethod
    def dex(cls) -> str:
        """Returns the official DEX name.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @classmethod
    @abstractmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @classmethod
    @abstractmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @abstractmethod
    def get_amount_out(self, asset: Assets, precise: bool) -> tuple[Assets, float]:
        """Returns the amount of output assets for a given input asset.

        Args:
            asset (Assets): The input assets.
            precise (bool): Whether to calculate precisely.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @abstractmethod
    def get_amount_in(self, asset: Assets, precise: bool) -> tuple[Assets, float]:
        """Returns the amount of input assets for a given output asset.

        Args:
            asset (Assets): The output assets.
            precise (bool): Whether to calculate precisely.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @property
    @abstractmethod
    def swap_forward(self) -> bool:
        """Indicates if swap forwarding is supported.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @property
    def inline_datum(self) -> bool:
        """Indicates if inline datum is used.

        Returns:
            bool: True if inline datum is used, False otherwise.
        """
        return self.plutus_v2

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Returns the reference UTXO.

        Returns:
            UTxO | None: The reference UTXO, or None if not available.
        """
        return None

    @property
    @abstractmethod
    def stake_address(self) -> Address:
        """Returns the stake address.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns the class of the order datum.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the default script class.

        Returns:
            type[PlutusV1Script] | type[PlutusV2Script]: The default script class.
        """
        return PlutusV1Script

    @classmethod
    def script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns the script class based on whether Plutus V2 is used.

        Returns:
            type[PlutusV1Script] | type[PlutusV2Script]: The script class.
        """
        if cls.plutus_v2:
            return PlutusV2Script
        return PlutusV1Script

    def swap_datum(  # noqa: PLR0913
        self,
        address_source: Address,  # noqa: ARG002
        in_assets: Assets,  # noqa: ARG002
        out_assets: Assets,  # noqa: ARG002
        extra_assets: Assets | None = None,  # noqa: ARG002
        address_target: Address | None = None,  # noqa: ARG002
        datum_target: PlutusData | None = None,  # noqa: ARG002
    ) -> PlutusData:
        """Creates the swap datum.

        Args:
            address_source (Address): The source address.
            in_assets (Assets): The input assets.
            out_assets (Assets): The output assets.
            extra_assets (Assets | None): Extra assets included in the transaction.
            address_target (Address | None): The target address.
            datum_target (PlutusData | None): The target datum.

        Raises:
            NotImplementedError: If the method is not implemented.

        Returns:
            PlutusData: The swap datum.
        """
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @abstractmethod
    def swap_utxo(  # noqa: PLR0913
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> TransactionOutput:
        """Creates the swap UTXO.

        Args:
            address_source (Address): The source address.
            in_assets (Assets): The input assets.
            out_assets (Assets): The output assets.
            extra_assets (Assets | None): Extra assets included in the transaction.
            address_target (Address | None): The target address.
            datum_target (PlutusData | None): The target datum.

        Raises:
            NotImplementedError: If the method is not implemented.

        Returns:
            TransactionOutput: The swap UTXO.
        """
        error_msg = "This method is not  implemented"
        raise NotImplementedError(error_msg)

    @property
    def volume_fee(self) -> int | None:
        """Returns the swap fee in basis points.

        Returns:
            int: The swap fee.
        """
        return self.fee

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        """Creates a cancel redeemer.

        Returns:
            PlutusData: The cancel redeemer.
        """
        return Redeemer(CancelRedeemer())

    def batcher_fee(
        self,
        in_assets: Assets | None = None,  # noqa: ARG002
        out_assets: Assets | None = None,  # noqa: ARG002
        extra_assets: Assets | None = None,  # noqa: ARG002
    ) -> Assets:
        """Returns the batcher fee.

        Args:
            in_assets (Assets | None): The input assets for the swap.
            out_assets (Assets | None): The output assets for the swap.
            extra_assets (Assets | None): Extra assets included in the transaction.

        Returns:
            Assets: The batcher fee.
        """
        return self._batcher

    def deposit(
        self,
        in_assets: Assets | None = None,  # noqa: ARG002
        out_assets: Assets | None = None,  # noqa: ARG002
    ) -> Assets:
        """Returns the deposit fee.

        Args:
            in_assets (Assets | None): The input assets for the deposit.
            out_assets (Assets | None): The output assets for the deposit.

        Returns:
            Assets: The deposit fee.
        """
        return self._deposit

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """Returns the DEX NFT policy.

        This should be the policy or policy+name of the DEX NFT.

        Returns:
            Optional[str]: The policy or policy+name of the DEX NFT, or None.
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
