from abc import ABC
from abc import abstractmethod

from pycardano import Address
from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import UTxO
from pydantic import BaseModel
from pydantic import model_validator

from cardex.dataclasses.datums import CancelRedeemer
from cardex.dataclasses.models import Assets
from cardex.dexs.errors import InvalidPoolError
from cardex.dexs.errors import NoAssetsError
from cardex.dexs.errors import NotAPoolError
from cardex.utility import Assets
from cardex.utility import asset_to_value


class AbstractOrder(BaseModel, ABC):
    assets: Assets
    block_time: int
    block_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False
    order_nft: Assets | None = None
    price_numerator: int
    price_denominator: int
    tx_index: int
    tx_hash: str

    _batcher_fee: Assets
    _datum_parsed: PlutusData
    _deposit_fee: Assets
    _volume_fee: int | None = None

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

    @property
    @abstractmethod
    def stake_address(self) -> Address:
        raise NotImplementedError

    @property
    @abstractmethod
    def order_datum_class(self) -> type[PlutusData]:
        raise NotImplementedError

    @classmethod
    def default_script_class(self) -> type[PlutusV2Script]:
        return PlutusV2Script

    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> PlutusData:
        if self.swap_forward and address_target is not None:
            print(f"{self.__class__.__name__} does not support swap forwarding.")

        return self.order_datum_class.create_datum(
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            batcher_fee=self.batcher_fee,
            volume_fee=self.volume_fee,
            address_target=address_target,
            datum_target=datum_target,
        )

    def add_swap_input(
        self,
        tx_builder: TransactionBuilder,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        in_utxos: UTxO | None = None,
    ) -> TransactionOutput:
        # Basic checks
        if len(in_assets) != 1 or len(out_assets) != 1:
            raise ValueError(
                "Only one asset can be supplied as input, "
                + "and one asset supplied as output.",
            )

        order_datum = self.swap_datum(
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            address_target=address_target,
            datum_target=datum_target,
        )

        in_assets.root["lovelace"] = (
            in_assets["lovelace"]
            + self.batcher_fee.quantity()
            + self.deposit.quantity()
        )

        if self.inline_datum:
            output = TransactionOutput(
                address=self.stake_address,
                amount=asset_to_value(in_assets),
                datum=order_datum,
            )
        else:
            output = TransactionOutput(
                address=self.stake_address,
                amount=asset_to_value(in_assets),
                datum_hash=order_datum.hash(),
            )

        return output, order_datum

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        return Redeemer(CancelRedeemer())

    @property
    def volume_fee(self) -> int:
        """Swap fee of swap in basis points."""
        return self.fee

    @property
    def batcher_fee(self) -> Assets:
        """Batcher fee."""
        return self._batcher

    @classmethod
    def skip_init(cls, values: dict[str, ...]) -> bool:
        """An initial check to determine if parsing should be carried out.

        Args:
            values: The pool initialization parameters.

        Returns:
            bool: If this returns True, initialization checks will get skipped.
        """
        return False

    @classmethod
    def post_init(cls, values: dict[str, ...]):
        """Post initialization checks.

        Args:
            values: The pool initialization parameters
        """
        assets = values["assets"]
        non_ada_assets = [a for a in assets if a != "lovelace"]

        if len(assets) == 2:
            # ADA pair
            assert (
                len(non_ada_assets) == 1
            ), f"Pool must only have 1 non-ADA asset: {values}"

        elif len(assets) == 3:
            # Non-ADA pair
            assert len(non_ada_assets) == 2, "Pool must only have 2 non-ADA assets."

            # Send the ADA token to the end
            values["assets"].root["lovelace"] = values["assets"].root.pop("lovelace")

        else:
            if len(assets) == 1 and "lovelace" in assets:
                raise NoAssetsError(
                    f"Invalid pool, only contains lovelace: assets={assets}",
                )
            else:
                raise InvalidPoolError(
                    f"Pool must have 2 or 3 assets except factor, NFT, and LP tokens: assets={assets}",
                )
        return values

    @model_validator(mode="before")
    def translate_address(cls, values):
        """The main validation function called when initialized.

        Args:
            values: The pool initialization values.

        Returns:
            The parsed/modified pool initialization values.
        """
        if "assets" in values:
            if values["assets"] is None:
                raise NoAssetsError("No assets in the pool.")
            elif not isinstance(values["assets"], Assets):
                values["assets"] = Assets(**values["assets"])

        if cls.skip_init(values):
            return values

        # Parse the pool datum
        try:
            datum = cls.pool_datum_class.from_cbor(values["datum_cbor"])
        except (DeserializeException, TypeError) as e:
            raise NotAPoolError(
                "Pool datum could not be deserialized: \n "
                + f"    error={e}\n"
                + f"    tx_hash={values['tx_hash']}\n"
                + f"    datum={values['datum_cbor']}\n",
            )

        # To help prevent edge cases, remove pool tokens while running other checks
        pair = Assets({})
        if datum.pool_pair() is not None:
            for token in datum.pool_pair():
                try:
                    pair.root.update({token: values["assets"].root.pop(token)})
                except KeyError:
                    raise InvalidPoolError(
                        "Pool does not contain expected asset.\n"
                        + f"    Expected: {token}\n"
                        + f"    Actual: {values['assets']}",
                    )

        dex_nft = cls.extract_dex_nft(values)

        lp_tokens = cls.extract_lp_tokens(values)

        pool_nft = cls.extract_pool_nft(values)

        # Add the pool tokens back in
        values["assets"].root.update(pair.root)

        cls.post_init(values)

        return values

    @property
    def unit_in(self) -> str:
        """Token name of asset A."""
        return self.assets.unit(0)

    @property
    def unit_out(self) -> str:
        """Token name of asset A."""
        return self.assets.unit(0)

    @property
    def max_in(self) -> Assets:
        self.assets.quantity()

    @property
    def max_out(self) -> Assets:
        """Reserve amount of asset A."""
        return self.assets.quantity(0)
