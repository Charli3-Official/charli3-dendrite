"""Danogo DEX Module."""

from dataclasses import dataclass
from decimal import Decimal
from typing import List
from typing import Type

from pycardano import PlutusData

from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_base import AbstractPoolState
from charli3_dendrite.utility import Assets
from charli3_dendrite.utility import naturalize_assets


@dataclass
class TupleAsset:
    """Python representation of Aiken: pub type TupleAsset = (PolicyId, AssetName)."""

    policy: bytes  # maps to Aiken Index 0 (PolicyId)
    asset_name: bytes  # maps to Aiken Index 1 (AssetName)

    @property
    def unit(self) -> str:
        """Returns the standard string representation of the asset unit."""
        if not self.policy:
            return "lovelace"
        return self.policy.hex() + self.asset_name.hex()

    @property
    def assets(self) -> Assets:
        """Converts the tuple asset into an Assets object with 0 quantity."""
        return Assets(root={self.unit: 0})

    @classmethod
    def from_list(cls, data: List[bytes]) -> "TupleAsset":
        """Instantiates a TupleAsset from a list of bytes."""
        if len(data) != 2:
            raise ValueError(
                "Aiken Tuple (PolicyId, AssetName) must have exactly 2 elements.",
            )
        return cls(policy=data[0], asset_name=data[1])


Basis = int


@dataclass
class PRational(PlutusData):
    """Python representation of Aiken: pub type PRational = { numerator: Int, denominator: Int }."""

    CONSTR_ID = 0

    numerator: int
    denominator: int


@dataclass
class ConcentratedPoolDatum(PoolDatum):
    """Danogo Concentrated Pool Datum structure.

    Matches the on-chain CBOR representation of the pool's state.
    """

    CONSTR_ID = 0

    token_x: List[bytes]
    token_y: List[bytes]
    lp_fee_rate: Basis
    platform_fee_x: int
    platform_fee_y: int
    total_swap_fee: int
    sqrt_lower_price: PRational
    sqrt_upper_price: PRational
    min_x_change: int
    min_y_change: int
    circulating_lp_token: int
    last_withdraw_epoch: int

    def pool_pair(self) -> Assets | None:
        """Return the asset pair associated with the pool as an Assets object."""
        asset_x = TupleAsset.from_list(self.token_x)
        asset_y = TupleAsset.from_list(self.token_y)
        return asset_x.assets + asset_y.assets


class ConcentratedPoolState(AbstractPoolState):
    """Represents the state of a Danogo Concentrated Liquidity pool."""

    @classmethod
    def dex(cls) -> str:
        """Unique identifier for the DEX."""
        return "Danogo Concentrated Liquidity"

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Criteria used by the Factory to find Danogo pools on-chain."""
        return PoolSelector(
            # TODO: Replace with actual Danogo Concentrated Liquidity Pool script addresses
            addresses=[
                "addr_test1xza0kzxecnkk35tnpmt9tskw2ucv32lzh8ha86er0kfk9sfj6zlvm8uucky8guq7rtjjcr8cn5nwkfus5yrsefwv4tcsnkgzhy",
            ],
            # TODO: Replace with the actual Policy ID of Danogo Concentrated Liquidity Pool NFTs
            assets=[
                "bafb08d9c4ed68d1730ed655c2ce5730c8abe2b9efd3eb237d9362c1cef80e0978314255e268caa437056febc2be47e17fa914dd98155b62f5f3ba3a",
            ],
        )

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool, typically the Pool NFT unit."""
        return self.pool_nft.unit()

    @classmethod
    def pool_datum_class(cls) -> Type[ConcentratedPoolDatum]:
        """Link to the custom Danogo datum for CBOR decoding."""
        return ConcentratedPoolDatum

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Price of assets based on Virtual Reserves (Xv, Yv).

        Returns:
            A `Tuple[Decimal, Decimal]` where the first `Decimal` is the price to buy
                1 of token B in units of token A, and the second `Decimal` is the price
                to buy 1 of token A in units of token B.
        """
        # 1. Get actual real reserves from UTXO (normalized to natural units)
        nat_assets = naturalize_assets(self.assets)

        # 2. Calculate available real reserves (deducting platform and swap fees)
        # !!! Note: ignoring minAda deduction for now
        x_reserve = (
            Decimal(nat_assets[self.unit_a])
            - Decimal(self.pool_datum.platform_fee_x)
            - Decimal(self.pool_datum.total_swap_fee)
        )

        y_reserve = Decimal(nat_assets[self.unit_b]) - Decimal(
            self.pool_datum.platform_fee_y,
        )

        # Ensure reserves don't drop below zero due to fees
        x_reserve = max(x_reserve, Decimal(0))
        y_reserve = max(y_reserve, Decimal(0))

        # 3. Calculate Virtual Reserves (Xv, Yv) using the current tick's price bounds
        xv, yv = calculate_xv_yv(
            int(x_reserve),
            int(y_reserve),
            self.pool_datum.sqrt_lower_price.numerator,
            self.pool_datum.sqrt_lower_price.denominator,
            self.pool_datum.sqrt_upper_price.numerator,
            self.pool_datum.sqrt_upper_price.denominator,
        )

        # 4. Safe check before division to prevent ZeroDivisionError
        if xv == 0 or yv == 0:
            return (Decimal(0), Decimal(0))

        # Price of B in units of A = Yv / Xv
        price_b_in_a = yv / xv

        # Price of A in units of B = Xv / Yv
        price_a_in_b = xv / yv

        return (price_b_in_a, price_a_in_b)

    @property
    def tvl(self) -> Decimal:
        """Calculates the Total Value Locked (TVL) denominated in ADA.

        Returns:
            Decimal: Total ADA value in the Pool (Lovelace / 1,000,000).
        """
        # 1. Normalize the assets in the UTXO to natural units (Decimal)
        nat_assets = naturalize_assets(self.assets)

        # 2. Get the actual Lovelace (ADA) amount present in the pool
        lovelace_amount = Decimal(nat_assets.get("lovelace", 0))

        # 3. Calculate TVL based on the trading pair type:

        # Case A: Pool contains ADA (e.g., ADA/USDM)
        # We assume the value of the other token is equivalent to the ADA present (50/50 split)
        if self.unit_a == "lovelace" or self.unit_b == "lovelace":
            # TVL = ADA Amount * 2
            # Divide by 1,000,000 to convert from Lovelace to ADA
            return (lovelace_amount * Decimal(2)) / Decimal(1_000_000)

        # Case B: Token-Token Pair (e.g., BTC/USDM)
        # The UTXO only holds a small amount of min-ADA to keep it alive.
        # We return this actual ADA amount. The higher-level Aggregator will
        # calculate the true value of BTC and USDM using prices from other pools.
        else:
            return lovelace_amount / Decimal(1_000_000)


def calculate_l(
    xR: Decimal,
    yR: Decimal,
    sqrt_pa: Decimal,
    sqrt_pb: Decimal,
) -> Decimal:
    """Calculate the liquidity (L) of the current tick using the concentrated liquidity formula.

    Args:
        xR: Real token X reserve amount.
        yR: Real token Y reserve amount.
        sqrt_pa: Square root of the lower price bound.
        sqrt_pb: Square root of the upper price bound.

    Returns:
        Decimal: The calculated liquidity L.
    """
    p_a = sqrt_pa * sqrt_pa
    p_b = sqrt_pb * sqrt_pb

    if p_a <= 0 or p_b <= 0:
        raise ValueError("Price values must be positive")

    t1 = xR * sqrt_pa * sqrt_pb

    # Calculate expression under the square root: (Y - Term 1)^2 + 4 * X * Y * Pb
    tmp = yR - t1
    under_sqrt = (tmp * tmp) + (Decimal(4) * xR * yR * p_b)
    t2 = under_sqrt.sqrt()

    num = yR + t1 + t2

    # Denominator: 2 * (sqrt(Pb) - sqrt(Pa))
    denom = Decimal(2) * (sqrt_pb - sqrt_pa)

    if denom == 0:
        raise ValueError("Price range (Pb - Pa) cannot be zero")

    return num / denom


def calculate_xv_yv(
    xR: int,
    yR: int,
    sqrt_pa_num: int,
    sqrt_pa_den: int,
    sqrt_pb_num: int,
    sqrt_pb_den: int,
) -> tuple[Decimal, Decimal]:
    """Calculate the virtual reserves (Xv, Yv) for the current price tick.

    Virtual reserves represent the theoretical token balances if the pool
    were a standard x * y = k AMM operating across all price ranges.

    Args:
        xR: Real token X reserve amount.
        yR: Real token Y reserve amount.
        sqrt_pa_num/den: Numerator/Denominator for sqrt lower price.
        sqrt_pb_num/den: Numerator/Denominator for sqrt upper price.

    Returns:
        tuple[Decimal, Decimal]: A tuple containing (Xv, Yv).
    """
    if sqrt_pa_den == 0 or sqrt_pb_den == 0:
        raise ValueError("Denominators cannot be zero")

    x_big = Decimal(xR)
    y_big = Decimal(yR)

    # Convert rational fractions to Decimals for accurate calculation
    sqrt_pa = Decimal(sqrt_pa_num) / Decimal(sqrt_pa_den)
    sqrt_pb = Decimal(sqrt_pb_num) / Decimal(sqrt_pb_den)

    # 1. Calculate the active liquidity (L)
    l_value = calculate_l(x_big, y_big, sqrt_pa, sqrt_pb)

    if sqrt_pb == 0:
        raise ValueError("sqrt_pb cannot be zero")

    # 2. Calculate Virtual Reserves based on real reserves and liquidity
    # Formula: Xv = X_real + (L / sqrt(Pb))
    xv = x_big + (l_value / sqrt_pb)

    # Formula: Yv = Y_real + (L * sqrt(Pa))
    yv = y_big + (l_value * sqrt_pa)

    return xv, yv
