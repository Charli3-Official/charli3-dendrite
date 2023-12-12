from abc import abstractmethod
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, RootModel, root_validator
from pycardano import PlutusData

from cardex.utility import Assets, InvalidPoolError, naturalize_assets, NotAPoolError


class AbstractPoolState(BaseModel):
    """A particular pool state, either current or historical."""

    tx_index: int
    tx_hash: str
    assets: Assets
    datum_hash: str
    pool_nft: Optional[Assets] = None
    dex_nft: Optional[Assets] = None
    lp_tokens: Optional[Assets] = None
    datum_cbor: Optional[str] = None
    _datum: Optional[PlutusData] = None
    _dex: str = ""
    fee: Optional[int] = None
    _deposit: Assets
    _batcher: Assets
    inactive: bool = False

    def volume_fee(self) -> int:
        """Swap fee of swap in basis points."""
        return self.fee

    def batcher_fee(self) -> Assets:
        """Batcher fee."""
        return self._batcher

    def deposit(self) -> Assets:
        """Batcher fee."""
        return self._deposit

    @abstractmethod
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.lp_tokens.unit()

    def dex(self) -> str:
        return self._dex

    @staticmethod
    def pool_policy() -> Union[str, List[str], None]:
        """The pool nft policy.

        This should be the policy or policy+name of the pool nft.

        If None, then the default pool nft check is skipped.

        Returns:
            Optional[str]: policy or policy+name of pool nft
        """
        return None

    @staticmethod
    def lp_policy() -> Union[str, List[str], None]:
        """The lp token policy.

        This should be the policy or policy+name of the lp tokens.

        If None, then the default lp token check is skipped.

        Returns:
            Optional[str]: policy or policy+name of lp tokens
        """
        return None

    @staticmethod
    def dex_policy() -> Union[str, List[str], None]:
        """The dex nft policy.

        This should be the policy or policy+name of the dex nft.

        If None, then the default dex nft check is skipped.

        Returns:
            Optional[str]: policy or policy+name of dex nft
        """
        return None

    @classmethod
    def extract_dex_nft(cls, values: Dict[str, Any]) -> Optional[Assets]:
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
        if cls.dex_policy() is None:
            dex_nft = None

        # If the dex nft is in the values, it's been parsed already
        elif "dex_nft" in values:
            assert any([p.startswith(cls.dex_policy()) for p in values["dex_nft"]])
            dex_nft = values["dex_nft"]

        # Check for the dex nft
        else:
            nfts = [asset for asset in assets if asset.startswith(cls.dex_policy())]
            if len(nfts) < 1:
                raise InvalidPoolError(
                    f"{cls.__name__}: Pool must have one DEX NFT token."
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
        if cls.pool_policy() is None:
            return None

        # If the pool nft is in the values, it's been parsed already
        elif "pool_nft" in values:
            assert any([p.startswith(cls.pool_policy()) for p in values["pool_nft"]])
            pool_nft = Assets(
                **{key: value for key, value in values["pool_nft"].items()}
            )

        # Check for the pool nft
        else:
            nfts = [asset for asset in assets if asset.startswith(cls.pool_policy())]
            if len(nfts) != 1:
                raise NotAPoolError(
                    f"{cls.__name__}: A pool must have one pool NFT token."
                )
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        assets = values["assets"]
        pool_id = pool_nft.unit()[len(cls.pool_policy()) :]
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
        if cls.lp_policy() is None:
            return None

        # If the pool nft is in the values, it's been parsed already
        elif "lp_tokens" in values:
            assert any([p.startswith(cls.lp_policy()) for p in values["lp_tokens"]])
            lp_tokens = values["lp_tokens"]

        # Check for the pool nft
        else:
            nfts = [asset for asset in assets if asset.startswith(cls.lp_policy())]
            if len(nfts) > 0:
                lp_tokens = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
                values["lp_tokens"] = lp_tokens
            else:
                lp_tokens = None
                values["lp_tokens"] = None

        return lp_tokens

    @classmethod
    def skip_init(cls, values: Dict[str, Any]) -> bool:
        """An initial check to determine if parsing should be carried out.

        Args:
            values: The pool initialization parameters.

        Returns:
            bool: If this returns True, initialization checks will get skipped.
        """
        return False

    @classmethod
    def post_init(cls, values: Dict[str, Any]):
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
            raise NotAPoolError(
                f"Pool must have 2 or 3 assets except factor, NFT, and LP tokens: {assets}"
            )
        return values

    @root_validator(pre=True)
    def translate_address(cls, values):  # noqa: D102
        """The main validation function called when initialized.

        Args:
            values: The pool initialization values.

        Returns:
            The parsed/modified pool initialization values.
        """
        if "assets" in values:
            if not isinstance(values["assets"], Assets):
                values["assets"] = Assets(**values["assets"])

        if cls.skip_init(values):
            return values

        dex_nft = cls.extract_dex_nft(values)

        lp_tokens = cls.extract_lp_tokens(values)

        pool_nft = cls.extract_pool_nft(values)

        cls.post_init(values)

        return values

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
    def price(self) -> Tuple[Decimal, Decimal]:
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
            1 / Decimal(10**6)
        )

        return tvl

    @property
    def pool_datum(self) -> Dict[str, Any]:
        """The pool state datum."""
        if not self.raw_datum:
            self.raw_datum = BlockfrostBackend.api().script_datum(
                self.datum_hash, return_type="json"
            )["json_value"]

        return self.raw_datum

    @abstractmethod
    def get_amount_out(self, asset: Assets) -> Tuple[Assets, float]:
        pass


class AbstractConstantProductPoolState(AbstractPoolState):
    def get_amount_out(self, asset: Assets) -> Tuple[Assets, float]:
        """Get the output asset amount given an input asset amount.

        Args:
            asset: An asset with a defined quantity.

        Returns:
            A tuple where the first value is the estimated asset returned from the swap
                and the second value is the price impact ratio.
        """
        assert len(asset) == 1, "Asset should only have one token."
        assert asset.unit() in [
            self.unit_a,
            self.unit_b,
        ], f"Asset {asset.unit} is invalid for pool {self.unit_a}-{self.unit_b}"

        if asset.unit() == self.unit_a:
            reserve_in, reserve_out = self.reserve_a, self.reserve_b
            unit_out = self.unit_b
        else:
            reserve_in, reserve_out = self.reserve_b, self.reserve_a
            unit_out = self.unit_a

        # Calculate the amount out
        fee_modifier = 10000 - self.volume_fee()
        numerator: int = asset.quantity() * fee_modifier * reserve_out
        denominator: int = asset.quantity() * fee_modifier + reserve_in * 10000
        amount_out = Assets(**{unit_out: numerator // denominator})

        if amount_out.quantity() == 0:
            return amount_out, 0

        # Calculate the price impact
        price_numerator: int = (
            reserve_out * asset.quantity() * denominator * fee_modifier
            - numerator * reserve_in * 10000
        )
        price_denominator: int = reserve_out * asset.quantity() * denominator * 10000
        price_impact: float = price_numerator / price_denominator

        return amount_out, price_impact
