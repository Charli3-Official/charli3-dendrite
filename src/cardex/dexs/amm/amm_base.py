from abc import abstractmethod
from decimal import Decimal

from cardex.dataclasses.models import Assets
from cardex.dexs.core.base import AbstractPairState
from cardex.dexs.core.errors import InvalidPoolError
from cardex.dexs.core.errors import NoAssetsError
from cardex.dexs.core.errors import NotAPoolError
from cardex.utility import Assets
from cardex.utility import asset_to_value
from cardex.utility import naturalize_assets
from pycardano import Address
from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import TransactionOutput
from pydantic import model_validator


class AbstractPoolState(AbstractPairState):
    datum_cbor: str
    datum_hash: str
    inactive: bool = False
    lp_tokens: Assets | None = None
    pool_nft: Assets | None = None
    tx_index: int
    tx_hash: str

    _batcher_fee: Assets
    _datum_parsed: PlutusData | None = None
    _deposit: Assets
    _volume_fee: int | None = None

    @property
    @abstractmethod
    def pool_id(self) -> str:
        """A unique identifier for the pool.

        This is a unique string differentiating this pool from every other pool on the
        dex, and is necessary for dexs that have more than one pool for a pair but with
        different fee structures.
        """
        raise NotImplementedError("Unique pool id is not specified.")

    @property
    @abstractmethod
    def pool_datum_class(self) -> type[PlutusData]:
        raise NotImplementedError

    @property
    def pool_datum(self) -> PlutusData:
        """The pool state datum."""
        return self.pool_datum_class.from_cbor(self.datum_cbor)

    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> PlutusData:
        if self.swap_forward and address_target is not None:
            print(f"{self.__class__.__name__} does not support swap forwarding.")

        return self.order_datum_class.create_datum(
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
        )

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
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
    @property
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
    @property
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
    def extract_dex_nft(cls, values: dict[str, ...]) -> Assets | None:
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
        if cls.dex_policy is None:
            dex_nft = None

        # If the dex nft is in the values, it's been parsed already
        elif "dex_nft" in values:
            if not any(
                any(p.startswith(d) for d in cls.dex_policy) for p in values["dex_nft"]
            ):
                raise NotAPoolError("Invalid DEX NFT")
            dex_nft = values["dex_nft"]

        # Check for the dex nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in cls.dex_policy)
            ]
            if len(nfts) < 1:
                raise NotAPoolError(
                    f"{cls.__name__}: Pool must have one DEX NFT token.",
                )
            dex_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["dex_nft"] = dex_nft

        return dex_nft

    @classmethod
    def extract_pool_nft(cls, values) -> Assets:
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
        if cls.pool_policy is None:
            return None

        # If the pool nft is in the values, it's been parsed already
        elif "pool_nft" in values:
            if not any(
                any(p.startswith(d) for d in cls.pool_policy)
                for p in values["pool_nft"]
            ):
                raise InvalidPoolError(f"{cls.__name__}: Invalid pool NFT: {values}")
            pool_nft = Assets(
                **{key: value for key, value in values["pool_nft"].items()},
            )

        # Check for the pool nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in cls.pool_policy)
            ]

            if len(nfts) != 1:
                raise InvalidPoolError(
                    f"{cls.__name__}: A pool must have one pool NFT token.",
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        assets = values["assets"]
        pool_id = pool_nft.unit()[len(cls.pool_policy) :]
        lps = [asset for asset in assets if asset.endswith(pool_id)]
        for lp in lps:
            assets.root.pop(lp)

        return pool_nft

    @classmethod
    def extract_lp_tokens(cls, values) -> Assets:
        """Extract the lp tokens from the UTXO.

        Some DEXs put lp tokens into the pool UTXO.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the pool nft.
        """
        assets = values["assets"]

        # If no pool policy id defined, return nothing
        if cls.lp_policy is None:
            return None

        # If the pool nft is in the values, it's been parsed already
        elif "lp_tokens" in values:
            if values["lp_tokens"] is not None:
                if not any(
                    any(p.startswith(d) for d in cls.lp_policy)
                    for p in values["lp_tokens"]
                ):
                    raise InvalidPoolError(
                        f"{cls.__name__}: Pool has invalid LP tokens.",
                    )
            lp_tokens = values["lp_tokens"]

        # Check for the pool nft
        else:
            nfts = [
                asset
                for asset in assets
                if any(asset.startswith(policy) for policy in cls.lp_policy)
            ]
            if len(nfts) > 0:
                lp_tokens = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
                values["lp_tokens"] = lp_tokens
            else:
                lp_tokens = None
                values["lp_tokens"] = None

        return lp_tokens

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
    def price(self) -> tuple[Decimal, Decimal]:
        """Price of assets.

        Returns:
            A `Tuple[Decimal, Decimal] where the first `Decimal` is the price to buy
                1 of token B in units of token A, and the second `Decimal` is the price
                to buy 1 of token A in units of token B.
        """
        nat_assets = naturalize_assets(self.assets)

        prices = (
            (nat_assets[self.unit_a] / nat_assets[self.unit_b]),
            (nat_assets[self.unit_b] / nat_assets[self.unit_a]),
        )

        return prices

    @property
    def tvl(self) -> Decimal:
        """Return the total value locked for the pool.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        if self.unit_a != "lovelace":
            raise NotImplementedError("tvl for non-ADA pools is not implemented.")

        tvl = 2 * (Decimal(self.reserve_a) / Decimal(10**6)).quantize(
            1 / Decimal(10**6),
        )

        return tvl
