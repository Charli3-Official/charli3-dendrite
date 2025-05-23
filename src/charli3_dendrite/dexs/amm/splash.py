"""Minswap DEX Module."""

from dataclasses import dataclass
from typing import Any
from typing import List
from typing import Union

from pycardano import Address
from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano.plutus import RawDatum

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import OrderType
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_types import AbstractCommonStableSwapPoolState
from charli3_dendrite.dexs.amm.amm_types import AbstractConstantProductPoolState
from charli3_dendrite.dexs.core.base import AbstractPairState
from charli3_dendrite.dexs.core.errors import InvalidLPError
from charli3_dendrite.dexs.core.errors import NotAPoolError
from charli3_dendrite.utility import Assets
from charli3_dendrite.utility import asset_to_value


@dataclass
class BoolFalse(PlutusData):
    CONSTR_ID = 0


@dataclass
class BoolTrue(PlutusData):
    CONSTR_ID = 1


@dataclass
class Rationale(PlutusData):
    """Rationale for Plutus data."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class SplashOrderDatum(OrderDatum):
    """Order Datum."""

    CONSTR_ID = 0

    tag: bytes
    beacon: bytes
    in_asset: AssetClass
    tradable_input: int
    cost_per_ex_step: int
    min_marginal_output: int
    output: AssetClass
    base_price: Rationale
    fee: int
    redeemer_address: PlutusFullAddress
    cancel_pkh: bytes
    permitted_executors: List[bytes]

    def address_source(self) -> Address:
        """This method should return the source address associated with the order."""
        return self.redeemer_address.to_address()

    def requested_amount(self) -> Assets:
        """This method should return the amount requested in the order."""
        asset = self.output.assets.unit()
        if asset == "":
            asset = "lovelace"
        return Assets(root={self.output.assets.unit(): self.min_marginal_output})

    def order_type(self) -> OrderType:
        """This method should return the type of the order."""
        return OrderType.swap


@dataclass
class SplashSSPPoolDatum(PoolDatum):
    """Pool Datum."""

    CONSTR_ID = 0

    pool_nft: AssetClass
    an2n: int
    asset_x: AssetClass
    asset_y: AssetClass
    multiplier_x: int
    multiplier_y: int
    lp_token: AssetClass
    ampl_coeff_if_editable: Union[BoolFalse, BoolTrue]
    lp_fee_is_editable: Union[BoolFalse, BoolTrue]
    lp_fee_num: int
    protocol_fee_num: int
    dao_stable_proxy_witness: bytes
    treasury_address: bytes
    protocol_fee_x: int
    protocol_fee_y: int

    def pool_pair(self) -> Assets | None:
        """Return the asset pair associated with the pool."""
        return self.asset_x.assets + self.asset_y.assets


@dataclass
class SplashCPPPoolDatum(PoolDatum):
    """The pool datum for the Spectrum DEX."""

    pool_nft: AssetClass
    asset_x: AssetClass
    asset_y: AssetClass
    lp_token: AssetClass
    pool_fee: int
    treasury_fee: int
    treasury_x: int
    treasury_y: int
    dao_policy: RawDatum
    lq_bound: int
    treasury_address: bytes

    def pool_pair(self) -> Assets | None:
        """Returns the pool pair assets if available."""
        return self.asset_x.assets + self.asset_y.assets


@dataclass
class SplashCPPBidirPoolDatum(PoolDatum):
    """The pool datum for the Spectrum DEX."""

    pool_nft: AssetClass
    asset_x: AssetClass
    asset_y: AssetClass
    lp_token: AssetClass
    pool_fee_x: int
    pool_fee_y: int
    treasury_fee: int
    treasury_x: int
    treasury_y: int
    dao_policy: RawDatum
    lq_bound: int
    treasury_address: bytes

    def pool_pair(self) -> Assets | None:
        """Returns the pool pair assets if available."""
        return self.asset_x.assets + self.asset_y.assets


@dataclass
class SwapAction(PlutusData):
    CONSTR_ID = 0

    context_values_list: List[int]


@dataclass
class PDAOAction(PlutusData):
    CONSTR_ID = 1


@dataclass
class SSPoolRedeemer(PlutusData):
    CONSTR_ID = 0

    pool_in_idx: int
    pool_out_idx: int
    action: Union[SwapAction, PDAOAction]

    def set_idx(self, tx_builder: TransactionBuilder):
        """Set the pool in and out indexes.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        # Set the input index
        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.self_index = key.index

        # Set the output index
        for i, txo in enumerate(tx_builder.outputs):
            if txo.address.payment_part.payload == contract_hash:
                pool_index = i
        assert pool_index is not None
        self.pool_out_idx = pool_index


@dataclass
class CPPoolRedeemer(PlutusData):
    CONSTR_ID = 0

    action: int
    self_index: int

    def set_idx(self, tx_builder: TransactionBuilder):
        """Set the pool in and out indexes.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        # Find other CPPoolRedeemers that are already set
        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.self_index = key.index


class SplashBaseState(AbstractPairState):
    @classmethod
    def dex(cls) -> str:
        return "Splash"

    @classmethod
    def order_selector(self) -> list[str]:
        return ["addr1w9ryamhgnuz6lau86sqytte2gz5rlktv2yce05e0h3207qssa8euj"]

    @property
    def stake_address(self) -> Address | None:
        return Address.decode(self.order_selector()[0])

    @property
    def swap_forward(self) -> bool:
        return False

    @classmethod
    def order_datum_class(cls):
        return SplashOrderDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns default script class of type PlutusV1Script or PlutusV2Script."""
        return PlutusV2Script

    @classmethod
    def script_class(cls) -> type[PlutusV2Script]:
        return PlutusV2Script

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
        pool_class = cls.pool_datum_class()
        datum: SplashSSPPoolDatum | SplashCPPPoolDatum = pool_class.from_cbor(
            values["datum_cbor"],
        )
        nft = datum.pool_nft.assets.unit()

        # If the pool nft is in the values, it's been parsed already
        if "pool_nft" in values:
            pool_nft = Assets(
                **dict(values["pool_nft"].items()),
            )
            if pool_nft.quantity() != 1 or not pool_nft.unit().lower().endswith(
                b"nft".hex(),
            ):
                raise NotAPoolError("A pool must have one pool NFT token.")

        # Check for the pool nft
        else:
            if nft not in assets:
                raise NotAPoolError("A pool must have one pool NFT token.")

            pool_nft = Assets(root={nft: assets.root.pop(nft)})

            values["pool_nft"] = pool_nft

        return pool_nft

    @classmethod
    def skip_init(cls, values) -> bool:
        if "pool_nft" in values and "lp_tokens" in values:
            order_class = cls.pool_datum_class()
            try:
                datum: SplashSSPPoolDatum | SplashCPPPoolDatum = order_class.from_cbor(
                    values["datum_cbor"],
                )
            except DeserializeException:
                raise NotAPoolError("Invalid datum")

            if datum.pool_nft.assets.unit() not in values["pool_nft"]:
                raise NotAPoolError("Invalid pool NFT")
            if datum.lp_token.assets.unit() not in values["lp_tokens"]:
                raise NotAPoolError("Invalid LP tokens")

            values["assets"] = Assets.model_validate(values["assets"])
            return True
        else:
            return False

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return self.pool_nft.unit()

    @classmethod
    def extract_lp_tokens(cls, values: dict[str, Any]) -> Assets:
        """Extract the lp tokens from the UTXO.

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: None or the pool nft.
        """
        assets = values["assets"]
        pool_class = cls.pool_datum_class()
        datum: SplashSSPPoolDatum | SplashCPPPoolDatum = pool_class.from_cbor(
            values["datum_cbor"],
        )
        lp_token = datum.lp_token.assets.unit()

        # If no pool policy id defined, return nothing
        if "lp_tokens" in values:
            lp_tokens = values["lp_tokens"]

        # Check for the pool nft
        else:
            if lp_token not in assets:
                raise InvalidLPError("A pool must have pool lp tokens.")

            else:
                lp_tokens = Assets(**{lp_token: assets.root.pop(lp_token)})

            values["lp_tokens"] = lp_tokens

        return lp_tokens


class SplashSSPState(SplashBaseState, AbstractCommonStableSwapPoolState):
    """Splash StableSwap Pool State."""

    fee: int = 30
    fee_basis: int = 100000
    _batcher = Assets(lovelace=0)
    _deposit = Assets(lovelace=0)

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=["addr1w9wnm7vle7al9q4aw63aw63wxz7aytnpc4h3gcjy0yufxwc3mr3e5"],
        )

    def _get_ann(self) -> int:
        """The modified amp value.

        This is the derived amp value (ann) from the original stableswap paper. This is
        implemented here as the default, but a common variant of this does not use the
        exponent. The alternative version is provided in the
        AbstractCommonStableSwapPoolState class. WingRiders uses this version.
        """
        return self.pool_datum.an2n // 4

    @classmethod
    def pool_datum_class(self) -> type[SplashSSPPoolDatum]:
        return SplashSSPPoolDatum

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        datum: SplashSSPPoolDatum = SplashSSPPoolDatum.from_cbor(values["datum_cbor"])

        values["fee"] = datum.lp_fee_num + datum.protocol_fee_num

        assets = values["assets"]
        assets.root[assets.unit(0)] -= datum.protocol_fee_x
        assets.root[assets.unit(1)] -= datum.protocol_fee_y

        cls.asset_mulitipliers = [
            datum.multiplier_x,
            datum.multiplier_y,
        ]

        # Verify pool is active
        # TODO: should be updated to match the validator:
        # https://github.com/splashprotocol/splash-core/blob/main/validators/stable_pool/pool.ak
        values["inactive"] = assets.quantity() < 100000000

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = False,
        fee_on_input: bool = False,
    ) -> tuple[Assets, float]:
        return super().get_amount_out(asset=asset, precise=precise, fee_on_input=False)

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = False,
        fee_on_input: bool = False,
    ) -> tuple[Assets, float]:
        return super().get_amount_in(asset=asset, precise=precise, fee_on_input=False)

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder | None = None,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        assert self.tx_hash is not None
        assert self.pool_nft is not None
        assert self.lp_tokens is not None
        assert tx_builder is not None

        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.pool_nft.unit()],
            addresses=self.pool_selector().addresses,
        )

        # Get the output assets
        out_assets, _ = self.get_amount_out(asset=in_assets)

        # Create the output redeemer
        redeemer = Redeemer(
            SSPoolRedeemer(
                pool_in_idx=0,
                pool_out_idx=0,
                action=SwapAction(context_values_list=[int(self._get_d())]),
            ),
        )

        # Create the pool input UTxO
        assets = self.assets + self.pool_nft + self.lp_tokens
        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(assets),
                datum=self.pool_datum,
            ),
        )

        # Create the pool output UTxO
        # TODO: add protocol and fees
        assets.root[in_assets.unit()] += in_assets.quantity()
        assets.root[out_assets.unit()] -= out_assets.quantity()
        txo = TransactionOutput(
            address=order_info[0].address,
            amount=asset_to_value(assets),
            datum=self.pool_datum,
        )

        # Add the script input
        pool_hash = Address.decode(order_info[0].address).payment_part.payload
        script = (
            get_backend()
            .get_script_from_address(
                Address(payment_part=ScriptHash(payload=pool_hash)),
            )
            .to_utxo()
        )
        tx_builder.add_script_input(utxo=input_utxo, script=script, redeemer=redeemer)

        return txo, self.pool_datum


class SplashCPPState(SplashBaseState, AbstractConstantProductPoolState):
    """Splash StableSwap Pool State."""

    fee: int = 30
    fee_basis: int = 100000
    _batcher = Assets(lovelace=0)
    _deposit = Assets(lovelace=0)

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=[
                "addr1w8cq97k066w4rd37wprvd4qrfxctzlyd6a67us2uv6hnenqrkvy2j",
                "addr1wxw7upjedpkr4wq839wf983jsnq3yg40l4cskzd7dy8eyng2qkdgn",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[SplashCPPPoolDatum]:
        return SplashCPPPoolDatum

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        datum: SplashCPPPoolDatum = SplashCPPPoolDatum.from_cbor(values["datum_cbor"])

        values["fee"] = 100000 - datum.pool_fee + datum.treasury_fee

        assets = values["assets"]
        assets.root[assets.unit(0)] -= datum.treasury_x
        assets.root[assets.unit(1)] -= datum.treasury_y

        # Verify pool is active
        # TODO: should be updated to match the validator:
        # https://github.com/splashprotocol/splash-core/blob/9fd951054ac7143de6acf491f36d1073e729ba90/plutarch-validators/WhalePoolsDex/PContracts/PPool.hs#L367
        values["inactive"] = assets.quantity() < 100000000

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        amount_out, float = super().get_amount_out(asset=asset, precise=precise)

        pool_datum = self.pool_datum
        fee = pool_datum.pool_fee - pool_datum.treasury_fee
        dx = asset.quantity()
        dxf = dx * fee
        rx = self.assets[asset.unit()]

        if asset.unit() == self.assets.unit():
            ry = self.assets.quantity(1) + pool_datum.treasury_y
            rx += pool_datum.treasury_x
        else:
            ry = self.assets.quantity(0) + pool_datum.treasury_x
            rx += pool_datum.treasury_y

        # Check to make sure the output passes invariant, and decrease until it does
        dy = -amount_out.quantity()
        dyf = dy * fee
        try:
            assert -dy * (rx * 100000 + dxf) <= ry * dxf
            assert -dx * (ry * 100000 + dyf) <= rx * dyf
        except AssertionError:
            amount_out.root[amount_out.unit()] = int(ry * dxf / (rx * 100000 + dxf))

            dy = -amount_out.quantity()
            dyf = dy * fee

            assert -dy * (rx * 100000 + dxf) <= ry * dxf
            assert -dx * (ry * 100000 + dyf) <= rx * dyf

        return amount_out, float

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder | None = None,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        assert self.tx_hash is not None
        assert self.pool_nft is not None
        assert self.lp_tokens is not None
        assert tx_builder is not None

        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.pool_nft.unit()],
            addresses=self.pool_selector().addresses,
        )

        # Get the output assets
        out_assets, _ = self.get_amount_out(asset=in_assets)

        # Create the output redeemer
        redeemer = Redeemer(
            CPPoolRedeemer(
                action=2,
                self_index=-1,
            ),
        )

        # Create the pool input UTxO
        pool_datum_class = self.pool_datum_class()
        pool_datum = pool_datum_class.from_cbor(
            self.pool_datum.to_cbor(),
        )
        assets = self.assets + self.pool_nft + self.lp_tokens
        assets.root[self.assets.unit()] += pool_datum.treasury_x
        assets.root[self.assets.unit(1)] += pool_datum.treasury_y
        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_info[0].address,
                amount=asset_to_value(assets),
                datum=self.pool_datum,
            ),
        )

        # Create the pool output UTxO
        new_assets = Assets.model_validate(assets.model_dump())
        new_assets.root[in_assets.unit()] += in_assets.quantity()
        new_assets.root[out_assets.unit()] += -out_assets.quantity()
        new_pool_datum = pool_datum_class.from_cbor(
            self.pool_datum.to_cbor(),
        )

        if in_assets.unit() == new_pool_datum.asset_x.assets.unit():
            new_pool_datum.treasury_x += int(
                in_assets.quantity() * new_pool_datum.treasury_fee // 100000,
            )
        elif in_assets.unit() == new_pool_datum.asset_y.assets.unit():
            new_pool_datum.treasury_y += int(
                in_assets.quantity() * new_pool_datum.treasury_fee // 100000,
            )
        else:
            raise ValueError("Invalid input asset")

        txo = TransactionOutput(
            address=order_info[0].address,
            amount=asset_to_value(new_assets),
            datum=new_pool_datum,
        )

        # Add the script input
        pool_hash = Address.decode(order_info[0].address).payment_part.payload
        script = (
            get_backend()
            .get_script_from_address(
                Address(payment_part=ScriptHash(payload=pool_hash)),
            )
            .to_utxo()
        )
        tx_builder.add_script_input(utxo=input_utxo, script=script, redeemer=redeemer)

        return txo, self.pool_datum


class SplashCPPBidirState(SplashCPPState):
    """Splash StableSwap Pool State."""

    fee: int = [30, 30]
    _batcher = Assets(lovelace=0)
    _deposit = Assets(lovelace=0)

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=[
                "addr1w95q755yrsr0xt8vmn007tpqee4hps49yjdef5dzknhl99qntsmh0",
            ],
        )

    @classmethod
    def pool_datum_class(self) -> type[SplashCPPBidirPoolDatum]:
        return SplashCPPBidirPoolDatum

    @classmethod
    def post_init(cls, values):
        super().post_init(values)

        datum: SplashCPPBidirPoolDatum = SplashCPPBidirPoolDatum.from_cbor(
            values["datum_cbor"],
        )

        values["fee"] = [
            (100000 - datum.pool_fee_x + datum.treasury_fee) // 10,
            (100000 - datum.pool_fee_y + datum.treasury_fee) // 10,
        ]

        assets = values["assets"]
        assets.root[assets.unit(0)] -= datum.treasury_x
        assets.root[assets.unit(1)] -= datum.treasury_y

        # Verify pool is active
        values["inactive"] = assets.quantity() < 100000000
