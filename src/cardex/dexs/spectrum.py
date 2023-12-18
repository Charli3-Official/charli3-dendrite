from dataclasses import dataclass
from typing import List

from pycardano import Address
from pycardano import PlutusData
from pycardano import TransactionOutput

from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusPartAddress
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import PoolSelector
from cardex.dexs.abstract_classes import AbstractConstantProductPoolState
from cardex.utility import InvalidLPError
from cardex.utility import InvalidPoolError
from cardex.utility import NotAPoolError


@dataclass
class SpectrumOrderDatum(PlutusData):
    CONSTR_ID = 0

    in_asset: AssetClass
    out_asset: AssetClass
    pool_token: AssetClass
    fee: int
    numerator: int
    denominator: int
    address_payment: bytes
    address_stake: PlutusPartAddress
    amount: int
    min_receive: int

    @classmethod
    def create_datum(
        cls,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        pool_token: Assets,
        batcher_fee: int,
        volume_fee: int,
    ) -> "SpectrumOrder":
        payment_part = bytes.fromhex(str(address.payment_part))
        stake_part = PlutusPartAddress(bytes.fromhex(str(address.staking_part)))
        in_asset = AssetClass.from_assets(in_assets)
        out_asset = AssetClass.from_assets(out_assets)
        pool = AssetClass.from_assets(pool_token)
        fee_mod = (10000 - volume_fee) // 10

        numerator, denominator = float.as_integer_ratio(
            batcher_fee / out_assets.quantity(),
        )

        return cls(
            in_asset=in_asset,
            out_asset=out_asset,
            pool_token=pool,
            fee=fee_mod,
            numerator=numerator,
            denominator=denominator,
            address_payment=payment_part,
            address_stake=stake_part,
            amount=in_assets.quantity(),
            min_receive=out_assets.quantity(),
        )


@dataclass
class SpectrumPoolDatum(PlutusData):
    CONSTR_ID = 0

    pool_nft: AssetClass
    asset_a: AssetClass
    asset_b: AssetClass
    pool_lq: AssetClass
    fee_mod: int
    maybe_address: List[bytes]
    lq_bound: int


class SpectrumCPPState(AbstractConstantProductPoolState):
    fee: int
    _batcher = Assets(lovelace=1500000)
    _deposit = Assets(lovelace=2000000)
    _stake_address = Address.from_primitive(
        "addr1wynp362vmvr8jtc946d3a3utqgclfdl5y9d3kn849e359hsskr20n",
    )

    @classmethod
    @property
    def dex(cls) -> str:
        return "Spectrum"

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            selector_type="addresses",
            selector=[
                "addr1x8nz307k3sr60gu0e47cmajssy4fmld7u493a4xztjrll0aj764lvrxdayh2ux30fl0ktuh27csgmpevdu89jlxppvrswgxsta",
                "addr1x94ec3t25egvhqy2n265xfhq882jxhkknurfe9ny4rl9k6dj764lvrxdayh2ux30fl0ktuh27csgmpevdu89jlxppvrst84slu",
            ],
        )

    @classmethod
    @property
    def order_datum_class(self) -> type[SpectrumOrderDatum]:
        return SpectrumOrderDatum

    @classmethod
    @property
    def pool_datum_class(self) -> type[SpectrumPoolDatum]:
        return SpectrumPoolDatum

    @property
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
                **{key: value for key, value in values["pool_nft"].items()},
            )
            if not pool_nft.endswith("nft"):
                raise NotAPoolError("A pool must have one pool NFT token.")

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
                raise InvalidLPError(
                    f"A pool must have pool lp tokens. Token names: {[bytes.fromhex(a[56:]) for a in assets]}",
                )

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

    def swap_tx_output(
        self,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        slippage: float = 0.005,
    ) -> tuple[TransactionOutput, SpectrumOrderDatum]:
        # Basic checks
        assert len(in_assets) == 1
        assert len(out_assets) == 1

        out_assets, _, _ = self.amount_out(in_assets, out_assets)
        out_assets.__root__[out_assets.unit()] = int(
            out_assets.__root__[out_assets.unit()] * (1 - slippage),
        )

        pool = self.get_pool_from_assets(in_assets + out_assets)

        order_datum = SpectrumOrder.create_datum(
            address=address,
            in_assets=in_assets,
            out_assets=out_assets,
            batcher_fee=pool.batcher_fee["lovelace"],
            volume_fee=pool.volume_fee,
            pool_token=pool.pool_nft,
        )

        deposit = (
            pool.batcher_fee["lovelace"]
            if in_assets.unit() == "lovelace"
            else pool.deposit_ada["lovelace"]
        )
        in_assets.__root__["lovelace"] = (
            in_assets["lovelace"] + pool.batcher_fee["lovelace"] + deposit
        )

        output = pycardano.TransactionOutput(
            address=STAKE_ORDER.address,
            amount=asset_to_value(in_assets),
            datum=order_datum,
        )

        return output, order_datum
