from dataclasses import dataclass
from typing import Union

from cardex.backend.dbsync import get_pool_in_tx
from cardex.backend.dbsync import get_script_from_address
from cardex.dataclasses.datums import AssetClass
from cardex.dataclasses.datums import PlutusFullAddress
from cardex.dataclasses.datums import PlutusNone
from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import PoolSelector
from cardex.dataclasses.models import PoolSelectorType
from cardex.dexs.ob.ob_base import AbstractOrderState
from cardex.utility import asset_to_value
from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO


@dataclass
class GeniusSubmitRedeemer(PlutusData):
    CONSTR_ID = 1
    spend_amount: int


@dataclass
class GeniusContainedFee(PlutusData):
    CONSTR_ID = 0
    lovelaces: int
    offered_tokens: int
    asked_tokens: int


@dataclass
class GeniusTimestamp(PlutusData):
    CONSTR_ID = 0
    timestamp: int


@dataclass
class GeniusNullTimestamp(PlutusData):
    CONSTR_ID = 0
    timestamp: int


@dataclass
class GeniusRational(PlutusData):
    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class GeniusYieldOrder(PlutusData):
    CONSTR_ID = 0
    owner_key: bytes
    owner_address: PlutusFullAddress
    offered_asset: AssetClass
    offered_original_amount: int
    offered_amount: int
    asked_asset: AssetClass
    price: GeniusRational
    nft: bytes
    start_time: Union[GeniusTimestamp, PlutusNone]
    end_time: Union[GeniusTimestamp, PlutusNone]
    partial_fills: int
    maker_lovelace_fee: int
    taker_lovelace_fee: int
    contained_fee: GeniusContainedFee
    contained_payment: int

    def pool_pair(self) -> Assets | None:
        return self.offered_asset.assets + self.asked_asset.assets


class GeniusYield(AbstractOrderState):
    """This class is largely used for OB dexes that allow direct script inputs."""

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False
    fee: int = 30 / 1.003

    _batcher_fee: Assets = Assets(lovelace=1000000)
    _datum_parsed: PlutusData | None = None

    @classmethod
    @property
    def dex_policy(cls) -> list[str] | None:
        """The dex nft policy.

        This should be the policy or policy+name of the dex nft.

        If None, then the default dex nft check is skipped.

        Returns:
            Optional[str]: policy or policy+name of dex nft
        """
        return [
            "22f6999d4effc0ade05f6e1a70b702c65d6b3cdf0e301e4a8267f585",
            "642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503",
        ]

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "GeniusYield"

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        out_check, _ = self.get_amount_out(in_assets)
        assert out_check.quantity() == out_assets.quantity()

        order_info = get_pool_in_tx(self.tx_hash, assets=[self.dex_nft.unit()])

        script = get_script_from_address(Address.decode(order_info[0].address))

        assets = self.assets + Assets(**{self.dex_nft.unit(): 1})
        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(assets),
                datum_hash=self.order_datum.hash(),
            ),
        )
        reference_utxo = UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=script.tx_index,
            ),
            output=TransactionOutput(
                address=script.address,
                amount=asset_to_value(script.assets),
                script=PlutusV2Script(bytes.fromhex(script.script)),
            ),
        )
        fee_reference_utxo = UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=0,
            ),
            output=TransactionOutput(
                address=script.address,
                amount=asset_to_value(
                    Assets(
                        **{
                            "lovelace": 2133450,
                            "fae686ea8f21d567841d703dea4d4221c2af071a6f2b433ff07c0af2682fd5d4b0d834a3aa219880fa193869b946ffb80dba5532abca0910c55ad5cd": 1,
                        },
                    ),
                ),
                datum=RawPlutusData.from_cbor(
                    "d8799f9f581cf43138a5c2f37cc8c074c90a5b347d7b2b3ebf729a44b9bbdc883787581c7a3c29ca42cc2d4856682a4564c776843e8b9135cf73c3ed9e986aba581c4fd090d48fceef9df09819f58c1d8d7cbf1b3556ca8414d3865a201c581cad27a6879d211d50225f7506534bbb3c8a47e66bbe78ef800dc7b3bcff03581c642c1f7bf79ca48c0f97239fcb2f3b42b92f2548184ab394e1e1e503d8799fd8799f581caf21fa93ded7a12960b09bd1bc95d007f90513be8977ca40c97582d7ffd87a80ff1a000f4240d8799f031903e8ff1a000f42401a00200b20ff",
                ),
            ),
        )
        tx_builder.add_script_input(
            utxo=input_utxo,
            script=reference_utxo,
            redeemer=Redeemer(
                GeniusSubmitRedeemer(spend_amount=out_assets.quantity() + 1),
            ),
        )

        tx_builder.reference_inputs = [
            fee_reference_utxo,
            list(tx_builder.reference_inputs)[0],
        ]

        print(tx_builder.reference_inputs)

        order_datum = self.order_datum_class.from_cbor(self.order_datum.to_cbor())
        order_datum.offered_amount -= out_assets.quantity() + 1
        order_datum.partial_fills += 1
        order_datum.contained_fee.lovelaces += 1000000
        order_datum.contained_fee.asked_tokens += (
            int(in_assets.quantity() * self.volume_fee) // 10000
        )
        order_datum.contained_payment += (
            int(in_assets.quantity() * (10000 - self.volume_fee)) // 10000
        ) + 1
        assets.root[in_assets.unit()] += in_assets.quantity()
        assets.root[out_assets.unit()] -= out_assets.quantity() + 1
        assets += self._batcher_fee
        txo = TransactionOutput(
            address=script.address,
            amount=asset_to_value(assets),
            datum_hash=order_datum.hash(),
        )

        tx_builder.datums.update({order_datum.hash(): order_datum})
        tx_builder.datums.update({self.order_datum.hash(): self.order_datum})

        # print(self.order_datum)
        print(order_datum.to_json(indent=1))

        return txo, order_datum

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return [
            "addr1wx5d0l6u7nq3wfcz3qmjlxkgu889kav2u9d8s5wyzes6frqktgru2",
            "addr1w8kllanr6dlut7t480zzytsd52l7pz4y3kcgxlfvx2ddavcshakwd",
        ]

    @classmethod
    @property
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        return PoolSelector(
            selector_type=PoolSelectorType.address,
            selector=cls.order_selector(),
        )

    @property
    def swap_forward(self) -> bool:
        return True

    @property
    def stake_address(self) -> Address | None:
        return None

    @classmethod
    @property
    def order_datum_class(self) -> type[PlutusData]:
        return GeniusYieldOrder

    @classmethod
    def default_script_class(self) -> type[PlutusV1Script] | type[PlutusV2Script]:
        return PlutusV2Script

    @property
    def price(self) -> tuple[int, int]:
        return [self.order_datum.price.numerator, self.order_datum.price.denominator]

    @property
    def available(self) -> Assets:
        """Max amount of output asset that can be used to fill the order."""
        return self.order_datum.offered_amount

    @property
    def tvl(self) -> int:
        """Return the total value locked in the order

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        return self.available

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob.

        Raises:
            NotImplementedError: Only ADA pool TVL is implemented.
        """
        return self.dex_nft
