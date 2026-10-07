"""CSwap DEX Module."""

from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import List
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import PlutusV3Script
from pycardano import Redeemer

from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_types import N_COINS
from charli3_dendrite.dexs.amm.amm_types import AbstractConstantProductPoolState
from charli3_dendrite.dexs.core.errors import NotAPoolError

# Protocol fee on the ADA leg of every swap, in basis points. It is charged by the
# batcher on top of the pool's own LP fee and does not stay in the pool.
CSWAP_PLATFORM_FEE = 15

POLICY_ID_LENGTH = 56
MIN_ADA_TARGET = 2_000_000  # minimum ADA an order's target carries
POOL_MAINTENANCE_ADA = 2_000_000  # lovelace a pool holds outside its reserves


def _platform_fee_amount(amount: int, fee_bps: int, fee_basis: int) -> int:
    """The platform fee charged on ``amount``: ``fee_bps`` of it, rounded up."""
    if amount <= 0 or fee_bps == 0:
        return 0
    return -(-amount * fee_bps // fee_basis)


def _gross_for_net(net: int, fee_bps: int, fee_basis: int) -> int:
    """The smallest ``amount`` whose net of the platform fee is at least ``net``.

    ``amount - ceil(amount * fee_bps / fee_basis)`` equals
    ``amount * (fee_basis - fee_bps) // fee_basis``, so its smallest pre-image is
    ``ceil(net * fee_basis / (fee_basis - fee_bps))``.
    """
    if net <= 0:
        return 0
    return -(-net * fee_basis // (fee_basis - fee_bps))


@dataclass
class CSwapOrderSwapType(PlutusData):
    """CSwap order type (Swap only)."""

    CONSTR_ID = 0


@dataclass
class CSwapOrderZapInType(PlutusData):
    """CSwap order type (Swap only)."""

    CONSTR_ID = 1


@dataclass
class CSwapOrderZapOutType(PlutusData):
    """CSwap order type (Swap only)."""

    CONSTR_ID = 2


@dataclass
class CSwapOrderDatum(OrderDatum):
    """CSwap order datum with ADA-only pair restriction."""

    CONSTR_ID = 0

    address: PlutusFullAddress  # Field 0: Complex address structure
    target_assets: List[List[Union[bytes, int]]]
    input_assets: List[List[Union[bytes, int]]]
    otype: Union[CSwapOrderSwapType | CSwapOrderZapInType | CSwapOrderZapOutType]
    slippage: int = 50
    platform_fee: int = CSWAP_PLATFORM_FEE

    @classmethod
    def create_datum(
        cls,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        batcher_fee: Assets,  # noqa: ARG003
        deposit: Assets,  # noqa: ARG003
        address_target: Address | None = None,  # noqa: ARG003
        datum_target: PlutusData | None = None,  # noqa: ARG003
    ) -> "CSwapOrderDatum":
        """Create a CSwap order datum."""
        # Validate ADA-only restriction
        merged_assets = in_assets + out_assets
        if "lovelace" not in merged_assets:
            raise ValueError("CSWAP only supports ADA pairs - one token must be ADA")

        full_address = PlutusFullAddress.from_address(address_source)

        # Create target assets list (what we want to receive)
        target_assets: list[list[bytes | int]] = []
        for unit in out_assets:
            if unit == "lovelace":
                target_assets.append([b"", b"", out_assets[unit]])
            else:
                policy = bytes.fromhex(unit[:POLICY_ID_LENGTH])
                name = bytes.fromhex(unit[POLICY_ID_LENGTH:])
                target_assets.append([policy, name, out_assets[unit]])

        # Add minimum ADA requirement (2 ADA minimum)
        if "lovelace" not in out_assets:
            target_assets.append([b"", b"", MIN_ADA_TARGET])

        # Create input assets list (always zero quantity for input)
        input_assets: list[list[bytes | int]] = []
        for unit in in_assets:
            if unit == "lovelace":
                input_assets.append([b"", b"", 0])
            else:
                policy = bytes.fromhex(unit[:POLICY_ID_LENGTH])
                name = bytes.fromhex(unit[POLICY_ID_LENGTH:])
                input_assets.append([policy, name, 0])

        return cls(
            address=full_address,
            target_assets=target_assets,
            input_assets=input_assets,
            otype=CSwapOrderSwapType(),
            slippage=50,  # 0.5% default slippage
            platform_fee=CSWAP_PLATFORM_FEE,
        )

    def address_source(self) -> Address:
        """Get the source address."""
        return self.address.to_address()

    def requested_amount(self) -> Assets:
        """Get the requested amount."""
        requested = {}
        for target in self.target_assets:
            raw_policy, raw_name, quantity = target[0], target[1], target[2]
            if not (
                isinstance(raw_policy, bytes)
                and isinstance(raw_name, bytes)
                and isinstance(quantity, int)
            ):
                raise ValueError("CSWAP target asset must be [policy, name, amount]")
            unit = (raw_policy + raw_name).hex() or "lovelace"
            if unit != "lovelace" or quantity > MIN_ADA_TARGET:  # Skip minimum ADA
                requested[unit] = quantity
        return Assets(requested)

    def order_type(self) -> OrderType:
        """Get the order type."""
        return OrderType.swap


@dataclass
class CSwapPoolDatum(PoolDatum):
    """CSwap pool datum with LP token tracking."""

    CONSTR_ID = 0

    total_lp_tokens: int  # Field 0: total lp tokens issued
    pool_fee: int  # Field 1: pool fee per 10K (85 = 0.85%)
    quote_policy: bytes  # Field 2: quote policy id - ADA (empty)
    quote_name: bytes  # Field 3: quote asset name - ADA (empty)
    base_policy: bytes  # Field 4: base policy id - token policy
    base_name: bytes  # Field 5: base asset name - token name
    lp_token_policy: bytes  # Field 6: lp token policy id
    lp_token_name: bytes  # Field 7: lp token asset name

    def pool_pair(self) -> Assets | None:
        """Return the pool pair assets."""
        quote_unit = "lovelace"
        base_unit = (self.base_policy + self.base_name).hex()
        if not base_unit:
            base_unit = "lovelace"

        return Assets(**{quote_unit: 0, base_unit: 0})


class CSwapCPPState(AbstractConstantProductPoolState):
    """CSwap CPP state with beacon token validation.

    A swap pays two fees. ``fee`` is the pool's LP fee (the datum ``pool_fee``),
    applied inside the constant-product curve. ``platform_fee`` is a protocol fee
    on the ADA leg, rounded up, that leaves the pool: on ADA in it is taken from
    the input before the curve, on ADA out from the curve's output before it is
    delivered.
    """

    fee: int = 85  # LP fee per 10K (0.85%), the datum pool_fee
    platform_fee: int = CSWAP_PLATFORM_FEE  # ADA-leg protocol fee per 10K
    _batcher = Assets(lovelace=690000)  # 0.69 ADA batcher fee
    _deposit = Assets(lovelace=2000000)  # 2 ADA deposit
    _stake_address: ClassVar[Address] = Address.decode(
        "addr1z8d9k3aw6w24eyfjacy809h68dv2rwnpw0arrfau98jk6nhv88awp8sgxk65d6kry0mar3rd0dlkfljz7dv64eu39vfs38yd9p",
    )

    @classmethod
    def dex(cls) -> str:
        """Get the DEX name."""
        return "CSWAP"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Get the order selector."""
        return [cls._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Get the pool selector."""
        return PoolSelector(
            addresses=[
                "addr1z8ke0c9p89rjfwmuh98jpt8ky74uy5mffjft3zlcld9h7ml3lmln3mwk0y3zsh3gs3dzqlwa9rjzrxawkwm4udw9axhs6fuu6e",
            ],
        )

    @property
    def swap_forward(self) -> bool:
        """Check if swap forwarding is enabled."""
        return False

    @property
    def stake_address(self) -> Address:
        """Get the stake address."""
        return self._stake_address

    @classmethod
    def order_datum_class(cls) -> type[CSwapOrderDatum]:
        """Get the order datum class."""
        return CSwapOrderDatum

    @classmethod
    def pool_datum_class(cls) -> type[CSwapPoolDatum]:
        """Get the pool datum class."""
        return CSwapPoolDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
        return f"cswap-{self.unit_a}-{self.unit_b}"

    @classmethod
    def extract_pool_nft(cls, values: dict[str, Any]) -> Assets | None:
        """Extract the CSwap pool NFT from the UTXO.

        CSwap uses a pool NFT system similar to Splash and Spectrum. The pool NFT:
        - Has name "c" (single character, hex: 63)
        - Has quantity of exactly 1
        - Policy ID varies between pools

        Args:
            values: The pool UTXO inputs.

        Returns:
            Assets: The pool NFT or None if not found.
        """
        assets = values["assets"]

        # If the pool NFT is already extracted, validate it
        if "pool_nft" in values:
            pool_nft = Assets(**dict(values["pool_nft"].items()))
            if pool_nft.quantity() != 1:
                raise NotAPoolError("CSWAP pool NFT must have quantity of exactly 1")

            # Check if token name is "c" (hex: 63)
            unit = pool_nft.unit()
            if len(unit) < POLICY_ID_LENGTH or unit[POLICY_ID_LENGTH:] != "63":
                raise NotAPoolError("CSWAP pool NFT must have name 'c'")

            return pool_nft

        # Search for pool NFT with name "c"
        pool_nft = None
        for asset_unit in assets:
            # Skip lovelace
            if asset_unit == "lovelace":
                continue

            # Check if token name is "c" (hex: 63)
            if (
                len(asset_unit) >= POLICY_ID_LENGTH
                and asset_unit[POLICY_ID_LENGTH:] == "63"
            ):
                quantity = assets[asset_unit]
                if quantity == 1:
                    pool_nft = Assets(root={asset_unit: assets.root.pop(asset_unit)})
                    break

        if pool_nft is None:
            raise NotAPoolError(
                "CSWAP pool must contain exactly one pool NFT with name 'c'",
            )

        values["pool_nft"] = pool_nft
        return pool_nft

    def _check_pair(self, asset: Assets) -> None:
        """Validate that ``asset`` is a single token of this ADA-paired pool."""
        if "lovelace" not in [self.unit_a, self.unit_b]:
            raise ValueError("CSWAP only supports ADA pairs - one token must be ADA")

        if asset.unit() not in [self.unit_a, self.unit_b]:
            raise ValueError(f"Asset {asset.unit()} not valid for this pool")

        if len(asset) != 1:
            raise ValueError("Only one asset can be provided for swap calculation")

    def _fee_modifier(self) -> int:
        """``fee_basis`` less the LP fee, the curve's input multiplier numerator."""
        if not isinstance(self.fee, int):
            raise TypeError("CSWAP pool fee must be an integer number of bp")
        return self.fee_basis - self.fee

    def platform_fee_bps(self, unit_in: str) -> tuple[int, int]:
        """The platform fee in bp on the (input, output) leg of a swap.

        The fee is charged on the ADA leg only: on the input when ``unit_in`` is
        lovelace, otherwise on the output.
        """
        if unit_in == "lovelace":
            return self.platform_fee, 0
        return 0, self.platform_fee

    def swap_amounts(self, unit_in: str, amount_in: int) -> tuple[int, int, int]:
        """The integer legs of swapping ``amount_in`` of ``unit_in``.

        Returns:
            ``(pool_in, pool_out, amount_out)``: the amount credited to the pool's
            input reserve, the amount debited from its output reserve, and the
            amount delivered to the swapper.
        """
        if unit_in == self.unit_a:
            reserve_in, reserve_out = self.reserve_a, self.reserve_b
        else:
            reserve_in, reserve_out = self.reserve_b, self.reserve_a
        fee_in, fee_out = self.platform_fee_bps(unit_in)

        pool_in = amount_in - _platform_fee_amount(amount_in, fee_in, self.fee_basis)
        pool_out = 0
        if pool_in > 0:
            fee_modifier = self._fee_modifier()
            pool_out = (pool_in * fee_modifier * reserve_out) // (
                pool_in * fee_modifier + reserve_in * self.fee_basis
            )
        amount_out = pool_out - _platform_fee_amount(pool_out, fee_out, self.fee_basis)
        return pool_in, pool_out, amount_out

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the output asset amount given an input asset amount.

        The LP fee is applied in the curve and the platform fee on the ADA leg
        (see ``swap_amounts``). The price impact is the curve's.
        """
        self._check_pair(asset)
        unit_in = asset.unit()
        unit_out = self.unit_b if unit_in == self.unit_a else self.unit_a

        pool_in, _, amount_out = self.swap_amounts(unit_in, asset.quantity())
        if amount_out <= 0:
            return Assets(**{unit_out: 0}), 0

        _, price_impact = super().get_amount_out(Assets(**{unit_in: pool_in}), precise)
        return Assets(**{unit_out: amount_out}), price_impact

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the smallest input whose ``get_amount_out`` reaches ``asset``.

        The curve output needed to deliver the request is grossed up for an
        ADA-out platform fee, the curve input for it is rounded up, and that is
        grossed up for an ADA-in platform fee. The price impact is the curve's.
        """
        self._check_pair(asset)
        unit_out = asset.unit()
        if unit_out == self.unit_a:
            unit_in, reserve_in, reserve_out = (
                self.unit_b,
                self.reserve_b,
                self.reserve_a,
            )
        else:
            unit_in, reserve_in, reserve_out = (
                self.unit_a,
                self.reserve_a,
                self.reserve_b,
            )
        fee_in, fee_out = self.platform_fee_bps(unit_in)

        if asset.quantity() <= 0:
            return Assets(**{unit_in: 0}), 0

        pool_out = _gross_for_net(asset.quantity(), fee_out, self.fee_basis)
        if pool_out >= reserve_out:
            return super().get_amount_in(Assets(**{unit_out: pool_out}), precise)

        pool_in = -(
            -(pool_out * self.fee_basis * reserve_in)
            // ((reserve_out - pool_out) * self._fee_modifier())
        )
        amount_in = _gross_for_net(pool_in, fee_in, self.fee_basis)

        _, price_impact = super().get_amount_in(
            Assets(**{unit_out: pool_out}),
            precise,
        )
        return Assets(**{unit_in: amount_in}), price_impact

    @classmethod
    def skip_init(cls, values: dict[str, Any]) -> bool:
        """Skip initialization if pool NFT is already present.

        Args:
            values: The pool UTXO inputs.

        Returns:
            bool: True if initialization should be skipped, False otherwise.
        """
        if "pool_nft" in values:
            # Pool NFT already extracted, just validate assets format
            if not isinstance(values["assets"], Assets):
                values["assets"] = Assets.model_validate(values["assets"])

            return True
        return False

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Post initialization for CSwap pools."""
        super().post_init(values)

        assets = values["assets"]

        # Validate this is an ADA pair
        asset_units = list(assets.root.keys())
        if "lovelace" not in asset_units:
            raise NotAPoolError("CSWAP pools must contain ADA (lovelace)")

        # Subtract 2 ADA pool maintenance from lovelace reserves
        # CSwap pools require 2 ADA minimum to maintain the pool
        if len(assets) == N_COINS:
            assets.root["lovelace"] -= POOL_MAINTENANCE_ADA

        # The datum carries the LP fee only; the platform fee is a protocol
        # constant charged separately on the ADA leg (see ``platform_fee``).
        if "datum_cbor" in values:
            try:
                datum = CSwapPoolDatum.from_cbor(values["datum_cbor"])
                values["fee"] = datum.pool_fee
            except Exception:  # noqa: BLE001
                # If datum parsing fails, use default fee
                values["fee"] = 85

        return values

    @classmethod
    def default_script_class(
        cls,
    ) -> type[PlutusV1Script] | type[PlutusV2Script] | type[PlutusV3Script]:
        """Get default script class as Plutus V3."""
        return PlutusV3Script

    @classmethod
    def cancel_redeemer(cls) -> PlutusData:
        """Returns the redeemer data for canceling transaction."""
        return Redeemer(CSwapOrderSwapType())
