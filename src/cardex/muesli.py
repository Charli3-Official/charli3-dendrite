from typing import Any, Dict, List, Optional

from cardex.base import AbstractConstantProductPoolState
from cardex.utility import Assets, InvalidPoolError, NotAPoolError

test_pool = (
    "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c",
)  # test pool policy


class MuesliswapCPPState(AbstractConstantProductPoolState):
    _dex = "muesliswap"
    fee: int = 30
    _batcher = Assets(lovelace=950000)
    _deposit = Assets(lovelace=1700000)

    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    def dex_policy() -> List[str]:
        return [
            "de9b756719341e79785aa13c164e7fe68c189ed04d61c9876b2fe53f4d7565736c69537761705f414d4d",
            "ffcdbb9155da0602280c04d8b36efde35e3416567f9241aff09552694d7565736c69537761705f414d4d",
            "f33bf12af1c23d660e29ebb0d3206b0bfc56ffd87ffafe2d36c42a454d7565736c69537761705f634c50",  # constant liquidity pools
            "a8512101cb1163cc218e616bb4d4070349a1c9395313f1323cc583634d7565736c695377617054657374506f6f6c",  # test pool policy
        ]

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
        if "dex_nft" in values:
            dex_nft = values["dex_nft"]

        # Check for the dex nft
        else:
            nfts = [asset for asset in assets if asset in cls.dex_policy()]
            if len(nfts) != 1:
                raise InvalidPoolError(
                    f"{cls.__name__}: Pool must have one DEX NFT token."
                )
            if test_pool in nfts:
                raise InvalidPoolError("This is a test pool.")
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

        # If the pool nft is in the values, it's been parsed already
        if "pool_nft" in values:
            pool_nft = Assets(
                **{key: value for key, value in values["pool_nft"].items()}
            )

        # Check for the pool nft
        else:
            nfts = [asset for asset, quantity in assets.items() if quantity == 1]
            if len(nfts) != 1:
                raise NotAPoolError("A pool must have one pool NFT token.")
            pool_nft = Assets(**{nfts[0]: assets.root.pop(nfts[0])})
            values["pool_nft"] = pool_nft

        return pool_nft
