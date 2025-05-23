"""Spectrum DEX Module."""

from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import List  # noqa: UP035
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import VerificationKeyHash

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.datums import PlutusPartAddress
from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_types import AbstractConstantProductPoolState
from charli3_dendrite.dexs.core.errors import InvalidLPError
from charli3_dendrite.dexs.core.errors import NotAPoolError


@dataclass
class SpectrumOrderDatum(OrderDatum):
    """The order datum for the Spectrum DEX."""

    in_asset: AssetClass
    out_asset: AssetClass
    pool_token: AssetClass
    fee: int
    numerator: int
    denominator: int
    address_payment: bytes
    address_stake: Union[PlutusPartAddress, PlutusNone]
    amount: int
    min_receive: int

    @classmethod
    def create_datum(
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        pool_token: Assets,
        batcher_fee: int,
        volume_fee: int,
    ) -> "SpectrumOrderDatum":
        """Create a Spectrum order datum."""
        payment_part = bytes.fromhex(str(address_source.payment_part))
        stake_part = PlutusPartAddress(bytes.fromhex(str(address_source.staking_part)))
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

    def address_source(self) -> Address:
        """Construct an Address object from the stored payment and stake parts.

        Returns:
            Address: An Address object containing the payment part and the staking part.
        """
        payment_part = VerificationKeyHash(self.address_payment)
        if isinstance(self.address_stake, PlutusNone):
            stake_part = None
        else:
            stake_part = VerificationKeyHash(self.address_stake.address)
        return Address(payment_part=payment_part, staking_part=stake_part)

    def requested_amount(self) -> Assets:
        """Calculate the requested amount of assets.

        Returns:
            Assets: An Assets object with the unit of the output asset
            and the minimum amount to receive.
        """
        return Assets({self.out_asset.assets.unit(): self.min_receive})

    def order_type(self) -> OrderType:
        """Retrieve the type of order.

        Returns:
            OrderType: The type of order, which is `OrderType.swap`.
        """
        return OrderType.swap


@dataclass
class SpectrumPoolDatum(PoolDatum):
    """The pool datum for the Spectrum DEX."""

    pool_nft: AssetClass
    asset_a: AssetClass
    asset_b: AssetClass
    pool_lq: AssetClass
    fee_mod: int
    maybe_address: List[bytes]
    lq_bound: int

    def pool_pair(self) -> Assets | None:
        """Returns the pool pair assets if available."""
        return self.asset_a.assets + self.asset_b.assets


@dataclass
class SpectrumCancelRedeemer(PlutusData):
    """The cancel redeemer for the Spectrum DEX."""

    CONSTR_ID = 0
    a: int
    b: int
    c: int
    d: int


class SpectrumCPPState(AbstractConstantProductPoolState):
    """The Spectrum DEX constant product pool state."""

    LEN_NAME_PARTS: ClassVar[int] = 3
    LEN_ASSETS: ClassVar[int] = 2

    fee: int = 0
    _batcher = Assets(lovelace=1500000)
    _deposit = Assets(lovelace=2000000)
    _stake_address: ClassVar[Address] = Address.from_primitive(
        "addr1wynp362vmvr8jtc946d3a3utqgclfdl5y9d3kn849e359hsskr20n",
    )
    _reference_utxo: ClassVar[UTxO | None] = None

    @classmethod
    def dex(cls) -> str:
        """Returns the name of the DEX ('Spectrum')."""
        return "Spectrum"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Returns the order selector list."""
        return [cls._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Returns the pool selector."""
        return PoolSelector(
            addresses=[
                "addr1x8nz307k3sr60gu0e47cmajssy4fmld7u493a4xztjrll0aj764lvrxdayh2ux30fl0ktuh27csgmpevdu89jlxppvrswgxsta",
                "addr1x94ec3t25egvhqy2n265xfhq882jxhkknurfe9ny4rl9k6dj764lvrxdayh2ux30fl0ktuh27csgmpevdu89jlxppvrst84slu",
            ],
        )

    @property
    def swap_forward(self) -> bool:
        """Returns if swap forwarding is enabled."""
        return False

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Retrieve the reference UTxO for the Spectrum DEX.

        This method checks if the reference UTxO is already set. If not, it retrieves
        the script bytes from the stake address and sets the reference UTxO.

        Returns:
            UTxO | None: The reference UTxO if available, otherwise None.
        """
        if cls._reference_utxo is None:
            script_reference = get_backend().get_script_from_address(cls._stake_address)

            if script_reference is None or script_reference.script is None:
                return None

            script_bytes = bytes.fromhex(script_reference.script)
            script = cls.default_script_class()(script_bytes)

            cls._reference_utxo = UTxO(
                input=TransactionInput(
                    transaction_id=TransactionId(
                        bytes.fromhex(
                            "fc9e99fd12a13a137725da61e57a410e36747d513b965993d92c32c67df9259a",
                        ),
                    ),
                    index=2,
                ),
                output=TransactionOutput(
                    address=Address.decode(
                        "addr1qxnwr9e72whcp3rnetaj3q34se8kvfqdxpwee6wlnysjt63lwrhst9wmcagdv46as9903ksvmdf7w7x6ujy4ap00yw0q85x25x",
                    ),
                    amount=Value(coin=12266340),
                    script=script,
                ),
            )

        return cls._reference_utxo

    @property
    def stake_address(self) -> Address:
        """Return the staking address."""
        return self._stake_address

    @classmethod
    def order_datum_class(cls) -> type[SpectrumOrderDatum]:
        """Returns data class used for handling order datums."""
        return SpectrumOrderDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Returns default script class of type PlutusV1Script or PlutusV2Script."""
        return PlutusV2Script

    @classmethod
    def pool_datum_class(cls) -> type[SpectrumPoolDatum]:
        """Returns data class, used for handling pool datums in the system.

        Returns:
            type[SpectrumPoolDatum]: class for Spectrum pool datums.
        """
        return SpectrumPoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        if self.pool_nft is None:
            raise ValueError("pool_nft is None, cannot get pool_id")
        return self.pool_nft.unit()

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

        # If the pool nft is in the values, it's been parsed already
        if "pool_nft" in values:
            pool_nft = Assets(
                **dict(values["pool_nft"].items()),
            )
            name = bytes.fromhex(pool_nft.unit()[56:]).split(b"_")
            if len(name) != cls.LEN_NAME_PARTS and name[2].decode().lower() != "nft":
                raise NotAPoolError("A pool must have one pool NFT token.")

        # Check for the pool nft
        else:
            pool_nft = None
            for asset in assets:
                name = bytes.fromhex(asset[56:]).split(b"_")
                if len(name) != cls.LEN_NAME_PARTS:
                    continue
                if name[2].decode().lower() == "nft":
                    pool_nft = Assets(**{asset: assets.root.pop(asset)})
                    break
            if pool_nft is None:
                raise NotAPoolError("A pool must have one pool NFT token.")

            values["pool_nft"] = pool_nft

        return pool_nft

    @classmethod
    def extract_lp_tokens(cls, values: dict[str, Any]) -> Assets:
        """Extract the lp tokens from the UTXO.

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
                if len(name) < cls.LEN_NAME_PARTS:
                    continue
                if name[2].decode().lower() == "lq":
                    lp_tokens = Assets(**{asset: assets.root.pop(asset)})
                    break
            if lp_tokens is None:
                raise InvalidLPError(
                    "A pool must have pool lp tokens. Token names: "
                    + str([bytes.fromhex(a[56:]) for a in assets]),
                )
            values["lp_tokens"] = lp_tokens

        return lp_tokens

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Post initialization checks.

        Args:
            values: The pool initialization parameters

        Returns:
            dict[str, Any]: The updated values dictionary.
        """
        super().post_init(values)

        # Check to see if the pool is active
        datum: SpectrumPoolDatum = SpectrumPoolDatum.from_cbor(values["datum_cbor"])

        assets = values["assets"]

        quantity = (
            assets.quantity() if len(assets) == cls.LEN_ASSETS else assets.quantity(1)
        )

        if 2 * quantity <= 1000000000:
            values["inactive"] = True

        values["fee"] = (1000 - datum.fee_mod) * 10
        return values

    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> PlutusData:
        """Create a PlutusData object representing a swap datum.

        Args:
            address_source (Address): The source address for the swap.
            in_assets (Assets): The input assets for the swap.
            out_assets (Assets): The output assets for the swap.
            extra_assets (Assets | None): Additional assets for the swap.
            Defaults to None.
            address_target (Address | None): The target address for the swap.
            Defaults to None.
            datum_target (PlutusData | None): The target datum for the swap.
            Defaults to None.

        Returns:
            PlutusData: A PlutusData object representing the swap datum.
        """
        if self.swap_forward and address_source is not None:
            print(  # noqa: T201
                f"{self.__class__.__name__} does not support swap forwarding.",
            )
        if self.pool_nft is None:
            raise ValueError("pool_nft is required but is None")

        volume_fee = int(
            self.volume_fee[0]
            if isinstance(self.volume_fee, list)
            else self.volume_fee or 0,
        )
        return SpectrumOrderDatum.create_datum(
            address_source=address_source,
            in_assets=in_assets,
            out_assets=out_assets,
            batcher_fee=self.batcher_fee(in_assets=in_assets, out_assets=out_assets)[
                "lovelace"
            ],
            volume_fee=volume_fee,
            pool_token=self.pool_nft,
        )

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        """Creates a cancel redeemer with predefined values.

        Returns:
            PlutusData: A PlutusData object representing the cancel redeemer.
        """
        return Redeemer(SpectrumCancelRedeemer(0, 0, 0, 1))
