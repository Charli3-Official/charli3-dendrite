import json
from dataclasses import dataclass
from typing import Dict, List

import cbor2
import pycardano
import requests

from cardex.base import AbstractConstantProductPoolState
from cardex.utility import AssetClass, Assets, InvalidPoolError, NotAPoolError


@dataclass
class SpectrumPoolDatum(pycardano.PlutusData):
    pool_nft: AssetClass
    asset_a: AssetClass
    asset_b: AssetClass
    pool_lq: AssetClass
    fee_mod: int
    maybe_address: List[bytes]
    lq_bound: int


class SpectrumCPPState(AbstractConstantProductPoolState):
    _dex = "spectrum"
    fee: int = 30
    _batcher = Assets(lovelace=2000000)
    _deposit = Assets(lovelace=2000000)

    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

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
            pool_nft = None
            for asset in assets:
                name = bytes.fromhex(asset[56:]).split(b"_")
                if len(name) != 3:
                    continue
                if name[2].decode().lower() == "nft":
                    pool_nft = Assets(**{asset: assets.root.pop(asset)})
                    break
            if pool_nft is None:
                raise NotAPoolError("A pool must have one pool NFT token.")

            values["pool_nft"] = pool_nft

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
        if "lp_tokens" in values:
            lp_tokens = values["lp_tokens"]

        # Check for the pool nft
        else:
            lp_tokens = None
            for asset in assets:
                name = bytes.fromhex(asset[56:]).split(b"_")
                if len(name) < 3:
                    continue
                if name[2].decode().lower() == "lq":
                    lp_tokens = Assets(**{asset: assets.root.pop(asset)})
                    break
            if lp_tokens is None:
                raise InvalidPoolError("A pool must have pool lp tokens.")

            values["lp_tokens"] = lp_tokens

        # Check to see if the pool is valid
        datum: SpectrumPoolDatum = SpectrumPoolDatum.from_cbor(values["datum_cbor"])

        # response = requests.post(
        #     "https://meta.spectrum.fi/cardano/minting/data/verifyPool/",
        #     headers={"Content-Type": "application/json"},
        #     data=json.dumps(
        #         [
        #             {
        #                 "nftCs": datum.pool_nft.policy.hex(),
        #                 "nftTn": datum.pool_nft.asset_name.hex(),
        #                 "lqCs": datum.pool_lq.policy.hex(),
        #                 "lqTn": datum.pool_lq.asset_name.hex(),
        #             }
        #         ]
        #     ),
        # ).json()
        # valid_pool = response[0][1]
        valid_pool = True

        if not valid_pool:
            raise InvalidPoolError

        if len(assets) == 2:
            quantity = assets.quantity()
        else:
            quantity = assets.quantity(1)

        if 2 * quantity <= datum.lq_bound:
            values["inactive"] = True

        values["fee"] = (1000 - datum.fee_mod) * 10

        return lp_tokens
