"""Minswap DEX Module."""

from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from hashlib import blake2b
from typing import Any
from typing import List
from typing import Union

import cbor2  # type: ignore[import-not-found]
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

LIQUIDITY_THRESHOLD = 100000000


@dataclass
class BoolFalse(PlutusData):
    """Plutus data representation of boolean false."""

    CONSTR_ID = 0


@dataclass
class BoolTrue(PlutusData):
    """Plutus data representation of boolean true."""

    CONSTR_ID = 1


@dataclass
class Rationale(PlutusData):
    """Rationale for Plutus data."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


def _canonical_plutus_cbor(data: PlutusData) -> bytes:
    """Serialise Plutus data to canonical, definite-length CBOR.

    pycardano encodes Plutus constructors with indefinite-length arrays. The
    Splash order contract recomputes the order beacon over the canonical
    (definite-length) encoding of the datum, so any hash that must agree with the
    on-chain validator has to be taken over this form rather than pycardano's
    default encoding.
    """
    raw = data.to_cbor()
    if isinstance(raw, str):
        raw = bytes.fromhex(raw)
    return cbor2.dumps(cbor2.loads(raw), canonical=True)


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
    permitted_executors: List[bytes] = field(
        default_factory=lambda: [
            b"\\\xb2\xc9h\xe5\xd1\xc7\x19zl\xe7aYg1\n7UE\xd9\xbce\x06:\x96C5\xb2",
        ],
    )

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

    def compute_beacon(
        self,
        seed_tx_hash: bytes | str,
        seed_index: int,
        order_index: int = 0,
    ) -> bytes:
        """Derive the 28-byte beacon that binds this order to its funding input.

        The beacon commits to the seed UTxO that funds the order
        (``seed_tx_hash`` at ``seed_index``), the order's position among the
        orders created in the same transaction (``order_index``), and the rest of
        the datum. The on-chain contract reproduces it as::

            blake2b_224(
                seed_tx_hash
                + seed_index.to_bytes(8, "big")
                + order_index.to_bytes(8, "big")
                + blake2b_224(canonical_cbor(datum with beacon = 28 zero bytes))
            )

        The inner digest is taken with the ``beacon`` field zeroed and over the
        canonical CBOR encoding, matching the validator.
        """
        if isinstance(seed_tx_hash, str):
            seed_tx_hash = bytes.fromhex(seed_tx_hash)

        placeholder = replace(self, beacon=bytes(28))
        inner = blake2b(
            _canonical_plutus_cbor(placeholder),
            digest_size=28,
        ).digest()
        preimage = (
            seed_tx_hash
            + seed_index.to_bytes(8, "big")
            + order_index.to_bytes(8, "big")
            + inner
        )
        return blake2b(preimage, digest_size=28).digest()

    def with_beacon(
        self,
        seed_tx_hash: bytes | str,
        seed_index: int,
        order_index: int = 0,
    ) -> "SplashOrderDatum":
        """Return a copy of this datum with the ``beacon`` field set correctly."""
        return replace(
            self,
            beacon=self.compute_beacon(seed_tx_hash, seed_index, order_index),
        )


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
class SplashCPPRoyaltyPoolDatum(PoolDatum):
    """The pool datum for the Spectrum DEX."""

    pool_nft: AssetClass
    asset_x: AssetClass
    asset_y: AssetClass
    lp_token: AssetClass
    pool_fee: int
    treasury_fee: int
    royalty_fee: int
    treasury_x: int
    treasury_y: int
    royalty_x: int
    royalty_y: int
    admin_address: RawDatum
    treasury_address: bytes
    royalty_pub_key: bytes
    nonce: int

    def pool_pair(self) -> Assets | None:
        """Returns the pool pair assets if available."""
        return self.asset_x.assets + self.asset_y.assets


@dataclass
class SwapAction(PlutusData):
    """Plutus data for swap action."""

    CONSTR_ID = 0

    context_values_list: List[int]


@dataclass
class PDAOAction(PlutusData):
    """Plutus data for PDAO action."""

    CONSTR_ID = 1


@dataclass
class SSPoolRedeemer(PlutusData):
    """Plutus data for stable swap pool."""

    CONSTR_ID = 0

    pool_in_idx: int
    pool_out_idx: int
    action: Union[SwapAction, PDAOAction]

    def set_idx(self, tx_builder: TransactionBuilder) -> None:
        """Set the pool in and out indexes.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        contract_hash = Address.from_primitive(
            "addr1w9wnm7vle7al9q4aw63aw63wxz7aytnpc4h3gcjy0yufxwc3mr3e5",
        ).payment_part.payload

        # Set the input index
        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.pool_in_idx = key.index

        # Set the output index
        for i, txo in enumerate(tx_builder.outputs):
            if txo.address.payment_part.payload == contract_hash:
                pool_index = i
        if pool_index is None:
            raise RuntimeError("Pool output not found in transaction outputs")
        self.pool_out_idx = pool_index


@dataclass
class CPPoolRedeemer(PlutusData):
    """Plutus data for constant product pool."""

    CONSTR_ID = 0

    action: int
    self_index: int

    def set_idx(self, tx_builder: TransactionBuilder) -> None:
        """Set the pool in and out indexes.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        # Find other CPPoolRedeemers that are already set
        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.self_index = key.index


@dataclass
class SplashCancelRedeemer(PlutusData):
    """Owner-reclaim redeemer for a Splash order spend (constructor 0).

    The base ``cancel_redeemer`` emits the executor action (constructor 1); an
    owner cancelling their own order spends with the owner-reclaim action
    (constructor 0).
    """

    CONSTR_ID = 0


class SplashBaseState(AbstractPairState):
    """Base state class for Splash DEX pools."""

    # Off-chain executor fee a Splash ORDER must pay to be picked up:
    # SplashOrderDatum.fee (2 ADA) + cost_per_ex_step (1 ADA) = 3 ADA. This is the
    # order/executor fee (used for fillability), DISTINCT from _batcher/_deposit
    # (=0) which the direct pool-spend swap path uses. Do NOT fold this into
    # _batcher — downstream consumers read _batcher as the direct-swap cost.
    _executor_fee = Assets(lovelace=3_000_000)

    @classmethod
    def dex(cls) -> str:
        """Return the name of the DEX."""
        return "Splash"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return the order selector addresses."""
        return ["addr1w9ryamhgnuz6lau86sqytte2gz5rlktv2yce05e0h3207qssa8euj"]

    def _reinstate_pool_lovelace(
        self,
        assets: Assets,
        pool_utxo_assets: Assets,
    ) -> None:
        """Preserve a token/token pool's min-UTxO ADA in the rebuilt pool value.

        When neither reserve is ADA, the pool still holds lovelace as min-UTxO. The
        reserve-bearing ``self.assets`` carries only the two token reserves (a lovelace
        key would sort to index 0 and corrupt the positional ``reserve_a``/``reserve_b``
        indexing), so a value rebuilt from it via ``asset_to_value`` has coin 0 and the
        builder defaults the recreated pool output to the protocol minimum, dropping the
        pool's real ADA. The pool validator requires that ADA preserved exactly across a
        swap (it lies outside the two swapped reserves and is unchanged), so copy the
        coin from the resolved on-chain pool UTxO into the rebuilt value. For an
        ADA-paired pool lovelace is a genuine reserve -- carried and updated by the swap
        -- so this is a no-op.
        """
        if self.unit_a == "lovelace" or self.unit_b == "lovelace":
            return
        assets.root["lovelace"] = pool_utxo_assets["lovelace"]

    @property
    def stake_address(self) -> Address | None:
        """Return the stake address for orders."""
        return Address.decode(self.order_selector()[0])

    @property
    def swap_forward(self) -> bool:
        """Return whether this DEX supports swap forwarding."""
        return False

    @classmethod
    def order_datum_class(cls) -> type[SplashOrderDatum]:
        """Return the order datum class for this DEX."""
        return SplashOrderDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns default script class of type PlutusV1Script or PlutusV2Script."""
        return PlutusV2Script

    @classmethod
    def script_class(cls) -> type[PlutusV2Script]:
        """Return the script class for this DEX."""
        return PlutusV2Script

    @classmethod
    def cancel_redeemer(cls) -> Redeemer:
        """Return the owner-reclaim redeemer for a Splash order spend.

        Overrides the base executor action (constructor 1) with the owner-reclaim
        action (constructor 0) used when the order owner cancels their own order.
        """
        return Redeemer(SplashCancelRedeemer())

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
    def skip_init(cls, values: dict[str, Any]) -> bool:
        """Check if initialization should be skipped for already parsed values."""
        if "pool_nft" in values and "lp_tokens" in values:
            order_class = cls.pool_datum_class()
            try:
                datum: (
                    SplashSSPPoolDatum
                    | SplashCPPPoolDatum
                    | SplashCPPRoyaltyPoolDatum
                    | SplashCPPBidirPoolDatum
                ) = order_class.from_cbor(
                    values["datum_cbor"],
                )
            except DeserializeException as err:
                raise NotAPoolError("Invalid datum") from err

            if datum.pool_nft.assets.unit() not in values["pool_nft"]:
                raise NotAPoolError("Invalid pool NFT")
            if datum.lp_token.assets.unit() not in values["lp_tokens"]:
                raise NotAPoolError("Invalid LP tokens")

            values["assets"] = Assets.model_validate(values["assets"])
            if len(values["assets"]) == 3:  # noqa: PLR2004
                lovelace = values["assets"].root.pop("lovelace")
                values["assets"].root["lovelace"] = lovelace

            return True

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
        """Return the pool selector for this DEX."""
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
    def pool_datum_class(cls) -> type[SplashSSPPoolDatum]:
        """Return the pool datum class for stable swap pools."""
        return SplashSSPPoolDatum

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> None:
        """Post-initialization processing for stable swap pools."""
        super().post_init(values)

        datum: SplashSSPPoolDatum = SplashSSPPoolDatum.from_cbor(values["datum_cbor"])

        values["fee"] = datum.lp_fee_num + datum.protocol_fee_num

        assets = values["assets"]
        assets.root[datum.asset_x.assets.unit()] -= datum.protocol_fee_x
        assets.root[datum.asset_y.assets.unit()] -= datum.protocol_fee_y

        if datum.asset_x.assets.unit() == assets.unit():
            cls.asset_mulitipliers = [
                datum.multiplier_x,
                datum.multiplier_y,
            ]
        else:
            cls.asset_mulitipliers = [
                datum.multiplier_y,
                datum.multiplier_x,
            ]

        # Verify pool is active
        # TODO: should be updated to match the validator:
        # https://github.com/splashprotocol/splash-core/blob/main/validators/stable_pool/pool.ak
        values["inactive"] = assets.quantity() < LIQUIDITY_THRESHOLD

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = False,
        fee_on_input: bool = False,
    ) -> tuple[Assets, float]:
        """Calculate output amount for a given input asset amount."""
        return super().get_amount_out(asset=asset, precise=precise, fee_on_input=False)

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = False,
        fee_on_input: bool = False,
    ) -> tuple[Assets, float]:
        """Calculate input amount required for a given output asset amount."""
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
        """Create a swap UTXO for the stable swap pool."""
        if self.tx_hash is None:
            raise ValueError("Transaction hash is required for swap operation")
        if self.pool_nft is None:
            raise ValueError("Pool NFT is required for swap operation")
        if self.lp_tokens is None:
            raise ValueError("LP tokens are required for swap operation")
        if tx_builder is None:
            raise ValueError("Transaction builder is required for swap operation")

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
        self._reinstate_pool_lovelace(assets, order_info[0].assets)
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

    fee: Union[int, list[int]] = 30
    fee_basis: int = 100000
    _batcher = Assets(lovelace=0)
    _deposit = Assets(lovelace=0)

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return the pool selector for constant product pools."""
        return PoolSelector(
            addresses=[
                "addr1w8cq97k066w4rd37wprvd4qrfxctzlyd6a67us2uv6hnenqrkvy2j",
                "addr1wxw7upjedpkr4wq839wf983jsnq3yg40l4cskzd7dy8eyng2qkdgn",
            ],
        )

    @classmethod
    def pool_datum_class(cls) -> type[SplashCPPPoolDatum]:
        """Return the pool datum class for constant product pools."""
        return SplashCPPPoolDatum

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> None:
        """Post-initialization processing for constant product pools."""
        super().post_init(values)

        datum: SplashCPPPoolDatum = cls.pool_datum_class().from_cbor(
            values["datum_cbor"],
        )

        values["fee"] = 100000 - datum.pool_fee + datum.treasury_fee

        assets = values["assets"]
        assets.root[datum.asset_x.assets.unit()] -= datum.treasury_x
        assets.root[datum.asset_y.assets.unit()] -= datum.treasury_y

        # Verify pool is active
        # TODO: should be updated to match the validator:
        # https://github.com/splashprotocol/splash-core/blob/9fd951054ac7143de6acf491f36d1073e729ba90/plutarch-validators/WhalePoolsDex/PContracts/PPool.hs#L367
        values["inactive"] = assets.quantity() < LIQUIDITY_THRESHOLD

    def _treasury_x(self) -> int:
        return self.pool_datum.treasury_x

    def _treasury_y(self) -> int:
        return self.pool_datum.treasury_y

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Calculate output amount for constant product pool with treasury fees."""
        amount_out, slippage = super().get_amount_out(asset=asset, precise=precise)

        if isinstance(self.fee, (int, float)):
            fee: int = self.fee
        else:
            fee = self.fee[0] if asset.unit() == self.assets.unit_a else self.fee[1]

        fee = 100000 - fee
        dx = asset.quantity()
        dxf = dx * fee
        rx = self.assets[asset.unit()]

        if asset.unit() == self.assets.unit():
            ry = self.assets.quantity(1)
        else:
            ry = self.assets.quantity(0)

        # Check to make sure the output passes invariant, and decrease until it does
        dy = -amount_out.quantity()
        dyf = dy * fee

        # First invariant check - if it fails, adjust the output amount
        if not (
            -dy * (rx * 100000 + dxf) <= ry * dxf
            and -dx * (ry * 100000 + dyf) <= rx * dyf
        ):
            amount_out.root[amount_out.unit()] = int(ry * dxf / (rx * 100000 + dxf))

            dy = -amount_out.quantity()
            dyf = dy * fee

            # Final invariant check - if this fails, it's a serious error
            if not (
                -dy * (rx * 100000 + dxf) <= ry * dxf
                and -dx * (ry * 100000 + dyf) <= rx * dyf
            ):
                raise ValueError(
                    "AMM violation: mathematical constraints cannot be satisfied. "
                    f"dy={dy}, dx={dx}, rx={rx}, ry={ry}, "
                    f"dxf={dxf}, dyf={dyf}",
                )

        return amount_out, slippage

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
        """Create a swap UTXO for the constant product pool."""
        if self.tx_hash is None:
            raise ValueError("Transaction hash is required for swap operation")
        if self.pool_nft is None:
            raise ValueError("Pool NFT is required for swap operation")
        if self.lp_tokens is None:
            raise ValueError("LP tokens are required for swap operation")
        if tx_builder is None:
            raise ValueError("Transaction builder is required for swap operation")

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
        self._reinstate_pool_lovelace(assets, order_info[0].assets)
        assets.root[pool_datum.asset_x.assets.unit()] += pool_datum.treasury_x
        assets.root[pool_datum.asset_y.assets.unit()] += pool_datum.treasury_y
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

    _batcher = Assets(lovelace=0)
    _deposit = Assets(lovelace=0)

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return the pool selector for bidirectional constant product pools."""
        return PoolSelector(
            addresses=[
                "addr1w95q755yrsr0xt8vmn007tpqee4hps49yjdef5dzknhl99qntsmh0",
            ],
        )

    @classmethod
    def pool_datum_class(cls) -> type[SplashCPPBidirPoolDatum]:
        """Return the pool datum class for bidirectional pools."""
        return SplashCPPBidirPoolDatum

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> None:
        """Post-initialization processing for bidirectional constant product pools."""
        super().post_init(values)

        datum: SplashCPPBidirPoolDatum = SplashCPPBidirPoolDatum.from_cbor(
            values["datum_cbor"],
        )

        if datum.asset_x.assets.unit() == values["assets"].unit():
            values["fee"] = [
                (100000 - datum.pool_fee_x + datum.treasury_fee),
                (100000 - datum.pool_fee_y + datum.treasury_fee),
            ]
        else:
            values["fee"] = [
                (100000 - datum.pool_fee_y + datum.treasury_fee),
                (100000 - datum.pool_fee_x + datum.treasury_fee),
            ]

        assets = values["assets"]
        assets.root[datum.asset_x.assets.unit()] -= datum.treasury_x
        assets.root[datum.asset_y.assets.unit()] -= datum.treasury_y

        # Verify pool is active
        values["inactive"] = assets.quantity() < LIQUIDITY_THRESHOLD


class SplashCPPRoyaltyState(SplashCPPState):
    """Splash StableSwap Pool State."""

    _batcher = Assets(lovelace=0)
    _deposit = Assets(lovelace=0)

    def _treasury_x(self) -> int:
        return self.pool_datum.treasury_x + self.pool_datum.royalty_x

    def _treasury_y(self) -> int:
        return self.pool_datum.treasury_y + self.pool_datum.royalty_y

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return the pool selector for royalty constant product pools."""
        return PoolSelector(
            addresses=[
                "addr1w89ksjnfu7ys02tedvslc9g2wk90tu5qte0dt4dge60hdugfqe64t",
            ],
        )

    @classmethod
    def pool_datum_class(cls) -> type[SplashCPPRoyaltyPoolDatum]:
        """Return the pool datum class for royalty pools."""
        return SplashCPPRoyaltyPoolDatum

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> None:
        """Post-initialization processing for royalty constant product pools."""
        super().post_init(values)

        datum: SplashCPPRoyaltyPoolDatum = SplashCPPRoyaltyPoolDatum.from_cbor(
            values["datum_cbor"],
        )

        values["fee"] += datum.royalty_fee

        assets = values["assets"]
        assets.root[datum.asset_x.assets.unit()] -= datum.royalty_x
        assets.root[datum.asset_y.assets.unit()] -= datum.royalty_y

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
        """Create a swap UTXO for the royalty constant product pool."""
        if self.tx_hash is None:
            raise ValueError("Transaction hash is required for swap operation")
        if self.pool_nft is None:
            raise ValueError("Pool NFT is required for swap operation")
        if self.lp_tokens is None:
            raise ValueError("LP tokens are required for swap operation")
        if tx_builder is None:
            raise ValueError("Transaction builder is required for swap operation")

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
        self._reinstate_pool_lovelace(assets, order_info[0].assets)
        assets.root[pool_datum.asset_x.assets.unit()] += (
            pool_datum.treasury_x + self.pool_datum.royalty_x
        )
        assets.root[pool_datum.asset_y.assets.unit()] += (
            pool_datum.treasury_y + self.pool_datum.royalty_y
        )
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
            new_pool_datum.royalty_x += int(
                in_assets.quantity() * new_pool_datum.royalty_fee // 100000,
            )
        elif in_assets.unit() == new_pool_datum.asset_y.assets.unit():
            new_pool_datum.treasury_y += int(
                in_assets.quantity() * new_pool_datum.treasury_fee // 100000,
            )
            new_pool_datum.royalty_y += int(
                in_assets.quantity() * new_pool_datum.royalty_fee // 100000,
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
