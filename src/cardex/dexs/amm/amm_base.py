"""AMM base module."""
from abc import abstractmethod
from decimal import Decimal
from typing import Any
from typing import Optional

from pycardano import Address
from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pydantic import model_validator

from cardex.dataclasses.models import Assets
from cardex.dexs.core.base import AbstractPairState
from cardex.dexs.core.constants import ONE_VALUE
from cardex.dexs.core.constants import THREE_VALUE
from cardex.dexs.core.constants import TWO_VALUE
from cardex.dexs.core.constants import ZERO_VALUE
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError
from cardex.utility import asset_to_value
from cardex.utility import naturalize_assets


class AbstractPoolState(AbstractPairState):
    """Abstract class representing the state of a pool in an exchange."""

    datum_cbor: str
    datum_hash: str
    inactive: bool = False
    lp_tokens: Assets | None = None
    pool_nft: Assets | None = None
    tx_index: int
    tx_hash: str

    _batcher_fee: Assets | None = None
    _datum_parsed: PlutusData | None = None
    _deposit: Assets | None = None
    _volume_fee: int | None = None

    @property
    @abstractmethod
    def pool_id(self) -> str:
        """A unique identifier for the pool.

        This is a unique string differentiating this pool from every other pool on the
        dex, and is necessary for dexs that have more than one pool for a pair but with
        different fee structures.
        """
        error_msg = "This method must be implemented by subclasses"
        raise NotImplementedError(error_msg)

    @classmethod
    @abstractmethod
    def pool_datum_class(cls) -> type[PlutusData]:
        """Abstract pool state datum.

        Raises:
        NotImplementedError: This method must be implemented by subclasses.

        Returns:
        type[PlutusData]: Class object of the PlutusData type representing pool state datum.
        """
        raise NotImplementedError

    @property
    def pool_datum(self) -> PlutusData:
        """The pool state datum."""
        return self.pool_datum_class().from_cbor(self.datum_cbor)

    def swap_utxo(  # noqa: PLR0913
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: Optional[TransactionBuilder] = None,  # noqa: ARG002
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Swap utxo that generates a transaction output representing the swap.

        Args:
            address_source (Address): The source address for the swap.
            in_assets (Assets): The assets to be swapped in.
            out_assets (Assets): The assets to be received after swapping.
            tx_builder (TransactionBuilder): Optional
            extra_assets (Assets, optional): Additional assets involved in the swap. Defaults to None.
            address_target (Address, optional): The target address for the swap. Defaults to None.
            datum_target (PlutusData, optional): The target datum for the swap. Defaults to None.

        Raises:
            ValueError: If more than one asset is supplied as input or output.

        Returns:
            Tuple[TransactionOutput, PlutusData]: The transaction output and the datum representing the swap operation.
        """
        # Basic checks
        if len(in_assets) != ONE_VALUE or len(out_assets) != ONE_VALUE:
            error_msg = "Only one asset can be supplied as input and as output."
            raise ValueError(error_msg)

        order_datum = self.swap_datum(
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            extra_assets=extra_assets,
            address_target=address_target,
            datum_target=datum_target,
        )

        in_assets.root["lovelace"] = (
            in_assets["lovelace"]
            + self.batcher_fee(
                in_assets=in_assets,
                out_assets=out_assets,
                extra_assets=extra_assets,
            ).quantity()
            + self.deposit(in_assets=in_assets, out_assets=out_assets).quantity()
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
    def pool_policy(cls) -> list[str] | None:
        """The pool nft policies.

        This should be the policy or policy+name of any pool nft policy that might be
        in the pool. Each pool must contain one of the NFTs in the list, and if this
        is None then no pool NFT check is made.

        By default, no pool policy is defined.

        Returns:
            Optional[List[str]]: list of policy or policy+name of pool nfts or None
        """
        return None

    @classmethod
    def lp_policy(cls) -> list[str] | None:
        """The lp token policies.

        Some dexs store staked lp tokens in the pool, and this definition is needed to
        filter out tokens from the assets.

        This should be the policy or policy+name of lp pool lp policy that might be
        in the pool. Each pool must contain one of the NFTs in the list, and if this
        is None then no lp token check is made.

        By default, no pool policy is defined.

        Returns:
            Optional[str]: policy or policy+name of lp tokens
        """
        return None

    @classmethod
    def extract_dex_nft(cls, values: dict[str, Any]) -> Assets | None:
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

        # If no dex policy id defined, return nothing
        dex_policy = cls.dex_policy()
        if dex_policy is None:
            return None

        # If the dex nft is in the values, it's been parsed already
        if "dex_nft" in values and values["dex_nft"] is not None:
            if not any(
                any(p.startswith(d) for d in dex_policy) for p in values["dex_nft"]
            ):
                error_msg = "Invalid DEX NFT"
                raise NotAPoolError(error_msg)
            dex_nft = values["dex_nft"]

        # Check for the dex nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in dex_policy)
            ]
            if len(nfts) < ONE_VALUE:
                error_msg = f"{cls.__name__}: Pool must have one DEX NFT token."
                raise NotAPoolError(error_msg)
            dex_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["dex_nft"] = dex_nft

        return dex_nft

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Assets | None:
        """Extract the pool nft from the UTXO.

        Some DEXs put a pool nft into the pool UTXO.

        This function checks to see if the pool nft is in the UTXO if the DEX policy is
        defined.

        If the pool nft is in the values, this value is skipped because it is assumed
        that this utxo has already been parsed.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the pool nft.
        """
        assets = values["assets"]

        # If no pool policy id defined, return nothing
        pool_policy = cls.pool_policy()
        if pool_policy is None:
            return None

        # If the pool nft is in the values, it's been parsed already
        if "pool_nft" in values:
            if values["pool_nft"] is not None and not any(
                any(p.startswith(d) for d in pool_policy) for p in values["pool_nft"]
            ):
                error_msg = f"{cls.__name__}: Invalid pool NFT: {values}"
                raise InvalidPoolError(error_msg)
            pool_nft = Assets(
                **dict(values["pool_nft"].items()),
            )

        # Check for the pool nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in pool_policy)
            ]
            if len(nfts) != ONE_VALUE:
                error_msg = f"{cls.__name__}: A pool must have one pool NFT token."
                raise InvalidPoolError(
                    error_msg,
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        assets = values["assets"]
        pool_id = pool_nft.unit()[len(pool_policy) :]
        lps = [asset for asset in assets if asset.endswith(pool_id)]
        for lp in lps:
            assets.root.pop(lp)

        return pool_nft

    @classmethod
    def extract_lp_tokens(cls, values: dict[str, Any]) -> Assets | None:
        """Extract the lp tokens from the UTXO.

        Some DEXs put lp tokens into the pool UTXO.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the pool nft.
        """
        assets = values["assets"]

        # If no pool policy id defined, return nothing
        lp_policy = cls.lp_policy()
        if lp_policy is None:
            return None

        # If the pool nft is in the values, it's been parsed already
        if "lp_tokens" in values:
            if values["lp_tokens"] is not None and not any(
                any(p.startswith(d) for d in lp_policy) for p in values["lp_tokens"]
            ):
                error_msg = f"{cls.__name__}: Pool has invalid LP tokens."
                raise InvalidPoolError(
                    error_msg,
                )
            lp_tokens = values["lp_tokens"]

        # Check for the pool nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in lp_policy)
            ]
            if len(nfts) > ZERO_VALUE:
                lp_tokens = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
                values["lp_tokens"] = lp_tokens
            else:
                lp_tokens = None
                values["lp_tokens"] = None

        return lp_tokens

    @classmethod
    def skip_init(cls, values: dict[str, Any]) -> bool:  # noqa: ARG003
        """An initial check to determine if parsing should be carried out.

        Args:
            values: The pool initialization parameters.

        Returns:
            bool: If this returns True, initialization checks will get skipped.
        """
        return False

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Post initialization checks.

        Args:
            values: The pool initialization parameters
        """
        assets = values["assets"]
        non_ada_assets = [a for a in assets if a != "lovelace"]

        if len(assets) == TWO_VALUE:
            if len(non_ada_assets) != ONE_VALUE:
                error_msg = f"Pool must only have 1 non-ADA asset: {values}"
                raise InvalidPoolError(error_msg)
            if len(assets) == THREE_VALUE and len(non_ada_assets) != THREE_VALUE:
                error_msg = f"Pool must only have 2 non-ADA assets: {values}"
                raise InvalidPoolError(error_msg)

            # Send the ADA token to the end
            values["assets"].root["lovelace"] = values["assets"].root.pop("lovelace")

        elif len(assets) == ONE_VALUE and "lovelace" in assets:
            error_msg = f"Invalid pool, only contains lovelace: assets={assets}"
            raise NoAssetsError(
                error_msg,
            )
        else:
            error_msg = f"Pool must have 2 or 3 assets except factor, NFT, and LP tokens: assets={assets}"
            raise InvalidPoolError(
                error_msg,
            )
        return values

    @model_validator(mode="before")
    @classmethod
    def translate_address(cls, values: dict[str, Any]) -> dict[str, Any]:
        """The main validation function called when initialized.

        Args:
            values: The pool initialization values.

        Returns:
            The parsed/modified pool initialization values.
        """
        if "assets" in values:
            if values["assets"] is None:
                error_msg = "No assets in the pool."
                raise NoAssetsError(error_msg)
            if not isinstance(values["assets"], Assets):
                values["assets"] = Assets(**values["assets"])

        if cls.skip_init(values):
            return values

        # Parse the pool datum
        try:
            datum = cls.pool_datum_class().from_cbor(values["datum_cbor"])
        except (DeserializeException, TypeError) as e:
            error_msg = (
                "Pool datum could not be deserialized: \n "
                + f" error={e}\n"
                + f"   tx_hash={values['tx_hash']}\n"
                + f"    datum={values['datum_cbor']}\n"
            )
            raise NotAPoolError(error_msg) from e

        # To help prevent edge cases, remove pool tokens while running other checks
        pair = Assets({})
        if datum.pool_pair() is not None:
            for token in datum.pool_pair():
                try:
                    pair.root.update({token: values["assets"].root.pop(token)})
                except KeyError:
                    error_msg = (
                        "Pool does not contain expected asset.\n"
                        + f"    Expected: {token}\n"
                        + f"    Actual: {values['assets']}"
                    )
                    raise InvalidPoolError(error_msg) from KeyError

        _ = cls.extract_dex_nft(values)

        _ = cls.extract_lp_tokens(values)

        _ = cls.extract_pool_nft(values)

        # Add the pool tokens back in
        values["assets"].root.update(pair.root)

        cls.post_init(values)

        return values

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Price of assets.

        Returns:
            A `Tuple[Decimal, Decimal] where the first `Decimal` is the price to buy
                1 of token B in units of token A, and the second `Decimal` is the price
                to buy 1 of token A in units of token B.
        """
        nat_assets = naturalize_assets(self.assets)

        return (
            (nat_assets[self.unit_a] / nat_assets[self.unit_b]),
            (nat_assets[self.unit_b] / nat_assets[self.unit_a]),
        )

    @property
    def tvl(self) -> Decimal:
        """Return the total value locked for the pool.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        if self.unit_a != "lovelace":
            error_msg = "tvl for non-ADA pools is not implemented."
            raise NotImplementedError(error_msg)

        return 2 * (Decimal(self.reserve_a) / Decimal(10**6)).quantize(
            1 / Decimal(10**6),
        )
