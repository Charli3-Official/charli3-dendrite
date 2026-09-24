"""Abstract base for pools whose reserves are an N-asset vector.

Every quote names its units explicitly: for ``N > 2`` there is no "other"
asset, so nothing on this base defaults or enumerates pairs.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod

from pycardano import Address
from pycardano import PlutusData
from pycardano import TransactionOutput
from pydantic import ConfigDict

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.utility import asset_to_value


class AbstractMultiAssetPoolState(DendriteBaseModel, ABC):
    """State of a pool priced over an N-entry reserve vector.

    ``reserves`` is keyed by dendrite unit in canonical order (lovelace first,
    then policy + name). Subclasses own the curve math and the order builders.
    """

    model_config = ConfigDict(
        alias_generator=None,
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    reserves: Assets

    @classmethod
    @abstractmethod
    def dex(cls) -> str:
        """The DEX name."""

    @classmethod
    @abstractmethod
    def pool_selector(cls) -> PoolSelector:
        """How pool UTxOs are found."""

    @classmethod
    @abstractmethod
    def order_selector(cls) -> list[str]:
        """The order submission addresses."""

    @property
    @abstractmethod
    def pool_id(self) -> str:
        """A unique identifier for the pool type."""

    @property
    @abstractmethod
    def stake_address(self) -> Address:
        """The address an order UTxO is locked at."""

    @abstractmethod
    def get_amount_out(
        self,
        asset: Assets,
        out_unit: str,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Output of ``out_unit`` for offering ``asset`` (one or more reserves)."""

    @abstractmethod
    def get_amount_in(
        self,
        asset: Assets,
        in_unit: str,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Minimum ``in_unit`` to obtain the single-asset ``asset``."""

    @abstractmethod
    def price(self, unit_in: str, unit_out: str) -> tuple[int, int]:
        """The exact ``(price_in, price_out)`` weights between two reserves."""

    @abstractmethod
    def apply_swap(self, asset_in: Assets, asset_out: Assets) -> None:
        """Move the reserves as if the swap settled."""

    @abstractmethod
    def batcher_fee(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
        extra_assets: Assets | None = None,
    ) -> Assets:
        """The lovelace an order locks for its execution fee."""

    @abstractmethod
    def deposit(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
    ) -> Assets:
        """The lovelace rider an order carries and gets back."""

    @abstractmethod
    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> PlutusData:
        """The order datum for a swap of ``in_assets`` into at least ``out_assets``."""

    def units(self) -> list[str]:
        """The reserve units in canonical order."""
        return list(self.reserves.root)

    def reserve(self, unit: str) -> int:
        """The declared reserve of ``unit``.

        Raises:
            KeyError: ``unit`` is not a reserve of this pool.
        """
        return self.reserves.root[unit]

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput, PlutusData]:
        """The order output for a swap: value = input + fee + rider, inline datum.

        Raises:
            ValueError: more than one asset offered or asked.
        """
        if len(in_assets) != 1 or len(out_assets) != 1:
            msg = "Only one asset can be supplied as input, and one asset as output."
            raise ValueError(msg)
        order_datum = self.swap_datum(
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            extra_assets=extra_assets,
            address_target=address_target,
            datum_target=datum_target,
        )
        value = Assets(**dict(in_assets.root))
        value.root["lovelace"] = (
            value.root.get("lovelace", 0)
            + self.batcher_fee(
                in_assets=in_assets,
                out_assets=out_assets,
                extra_assets=extra_assets,
            ).quantity()
            + self.deposit(in_assets=in_assets, out_assets=out_assets).quantity()
        )
        output = TransactionOutput(
            address=self.stake_address,
            amount=asset_to_value(value),
            datum=order_datum,
        )
        return output, order_datum
