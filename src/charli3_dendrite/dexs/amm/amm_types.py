"""Module providing types and state classes for AMM pools."""
import math
from typing import ClassVar

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.amm_base import AbstractPoolState

N_COINS = 2


class AbstractConstantProductPoolState(AbstractPoolState):
    """Represents the state of a constant product automated market maker (AMM) pool."""

    fee_basis: int = 10000

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the output asset amount given an input asset amount.

        Args:
            asset (Assets): An asset with a defined quantity.
            precise (bool): Whether to return precise calculations.

        Returns:
            A tuple where the first value is the estimated asset returned from the swap
                and the second value is the price impact ratio.
        """
        if len(asset) != 1:
            error_msg = "Asset should only have one token."
            raise ValueError(error_msg)
        if asset.unit() not in [self.unit_a, self.unit_b]:
            error_msg = (
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}"
            )
            raise ValueError(error_msg)

        if asset.unit() == self.unit_a:
            reserve_in, reserve_out = self.reserve_a, self.reserve_b
            unit_out = self.unit_b
        else:
            reserve_in, reserve_out = self.reserve_b, self.reserve_a
            unit_out = self.unit_a

        volume_fee: int = 0
        if self.volume_fee is not None:
            if isinstance(self.volume_fee, int):
                volume_fee = self.volume_fee
            elif asset.unit() == self.unit_a:
                volume_fee = self.volume_fee[0]
            else:
                volume_fee = self.volume_fee[1]

        # Calculate the amount out
        fee_modifier = self.fee_basis - volume_fee
        numerator: int = asset.quantity() * fee_modifier * reserve_out
        denominator: int = asset.quantity() * fee_modifier + reserve_in * self.fee_basis
        amount_out = Assets(**{unit_out: numerator // denominator})
        if not precise:
            amount_out.root[unit_out] = numerator // denominator

        if amount_out.quantity() == 0:
            return amount_out, 0

        # Calculate the price impact
        price_numerator: int = (
            reserve_out * asset.quantity() * denominator * fee_modifier
            - numerator * reserve_in * self.fee_basis
        )
        price_denominator: int = (
            reserve_out * asset.quantity() * denominator * self.fee_basis
        )
        price_impact: float = price_numerator / price_denominator

        return amount_out, price_impact

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the input asset amount given a desired output asset amount.

        Args:
            asset (Assets): An asset with a defined quantity.
            precise (bool): Whether to return precise calculations.

        Returns:
            The estimated asset needed for input in the swap.
        """
        if len(asset) != 1:
            error_msg = "Asset should only have one token."
            raise ValueError(error_msg)
        if asset.unit() not in [self.unit_a, self.unit_b]:
            error_msg = (
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}"
            )
            raise ValueError(error_msg)

        if asset.unit() == self.unit_b:
            reserve_in, reserve_out = self.reserve_a, self.reserve_b
            unit_out = self.unit_a
        else:
            reserve_in, reserve_out = self.reserve_b, self.reserve_a
            unit_out = self.unit_b

        volume_fee: int = 0
        if self.volume_fee is not None:
            if isinstance(self.volume_fee, int):
                volume_fee = self.volume_fee
            elif asset.unit() == self.unit_b:
                volume_fee = self.volume_fee[0]
            else:
                volume_fee = self.volume_fee[1]

        # Estimate the required input
        fee_modifier = self.fee_basis - volume_fee
        numerator: int = asset.quantity() * self.fee_basis * reserve_in
        denominator: int = (reserve_out - asset.quantity()) * fee_modifier
        amount_in = Assets(**{unit_out: numerator // denominator})
        if not precise:
            amount_in.root[unit_out] = numerator // denominator

        # Estimate the price impact
        price_numerator: int = (
            reserve_out * numerator * fee_modifier
            - asset.quantity() * denominator * reserve_in * self.fee_basis
        )
        price_denominator: int = reserve_out * numerator * self.fee_basis
        price_impact: float = price_numerator / price_denominator

        return amount_in, price_impact


class AbstractStableSwapPoolState(AbstractPoolState):
    """Represents the state of a stable swap automated market maker (AMM) pool."""

    asset_mulitipliers: ClassVar[list[int]] = [1, 1]
    fee_basis: int = 10000

    @property
    def reserve_a(self) -> int:
        """Reserve amount of asset A."""
        return self.assets.quantity(0) * self.asset_mulitipliers[0]

    @property
    def reserve_b(self) -> int:
        """Reserve amount of asset B."""
        return self.assets.quantity(1) * self.asset_mulitipliers[1]

    @property
    def amp(self) -> int:
        """Amplification coefficient used in the stable swap algorithm."""
        return 75

    def _get_ann(self) -> int:
        """The modified amp value.

        This is the derived amp value (ann) from the original stableswap paper. This is
        implemented here as the default, but a common variant of this does not use the
        exponent. The alternative version is provided in the
        AbstractCommonStableSwapPoolState class. WingRiders uses this version.
        """
        return self.amp * N_COINS**N_COINS

    def _get_d(self) -> float:
        """Regression to learn the stability constant."""
        # TODO: Expand this to operate on pools with more than one stable
        ann = self._get_ann()
        s = self.reserve_a + self.reserve_b
        if s == 0:
            return 0

        # Iterate until the change in value is <1 unit.
        d = s
        for _ in range(256):
            d_p = d**3 / (N_COINS**N_COINS * self.reserve_a * self.reserve_b)
            d_prev = d
            d = d * (ann * s + d_p * N_COINS) / ((ann - 1) * d + (N_COINS + 1) * d_p)

            if abs(d - d_prev) < 1:
                break

        return math.ceil(d)

    def _get_y(
        self,
        in_assets: Assets,
        out_unit: str,
        precise: bool = True,
        get_input: bool = False,
    ) -> Assets:
        """Calculate the output amount using a regression."""
        ann = self._get_ann()
        d = self._get_d()

        subtract = -1 if get_input else 1

        # Make sure only one input supplied
        if len(in_assets) > 1:
            error_msg = "Only one input asset allowed."
            raise ValueError(error_msg)
        if in_assets.unit() not in [self.unit_a, self.unit_b]:
            error_msg = "Invalid input token."
            raise ValueError(error_msg)
        if out_unit not in [self.unit_a, self.unit_b]:
            error_msg = "Invalid output token."
            raise ValueError(error_msg)

        in_quantity = in_assets.quantity()
        if in_assets.unit() == self.unit_a:
            in_reserve = (
                self.reserve_a + in_quantity * self.asset_mulitipliers[0] * subtract
            )
            out_multiplier = self.asset_mulitipliers[1]
        else:
            in_reserve = (
                self.reserve_b + in_quantity * self.asset_mulitipliers[1] * subtract
            )
            out_multiplier = self.asset_mulitipliers[0]

        s = in_reserve
        c = d**3 / (N_COINS**2 * ann * in_reserve)
        b = s + d / ann
        out_prev = 0
        out = d

        for _ in range(256):
            out_prev = int(out)
            out = (out**2 + c) / (2 * out + b - d)

            if abs(out - out_prev) < 1:
                break

        out /= out_multiplier
        out_assets = Assets(**{out_unit: int(out)})
        if not precise:
            out_assets.root[out_unit] = out

        return out_assets

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
        fee_on_input: bool = True,
    ) -> tuple[Assets, float]:
        """Calculate the amount of assets received when swapping a given input amount.

        This function computes the output amount for a swap operation in the
        stable swap pool, taking into account the volume fee and precision settings.

        Args:
            asset (Assets): The input asset amount for the swap.
            precise (bool): If True, returns precise integer output. Default True.
            fee_on_input (bool): If True, applies the fee to the input amount.
                                        If False, applies the fee to the output amount.
                                        Defaults to True.

        Returns:
            tuple[Assets, float]: A tuple containing:
                - The output asset amount after the swap.
                - A float value (always 0 in this implementation).

        Raises:
            ValueError: If the input asset is invalid or if multiple input
              assets are provided.
        """
        volume_fee: int = 0
        if self.volume_fee is not None:
            if isinstance(self.volume_fee, (int, float)):
                volume_fee = self.volume_fee
            elif asset.unit() == self.unit_a:
                volume_fee = self.volume_fee[0]
            else:
                volume_fee = self.volume_fee[1]

        if fee_on_input:
            in_asset = Assets(
                **{
                    asset.unit(): int(
                        asset.quantity()
                        * (self.fee_basis - volume_fee)
                        / self.fee_basis,
                    ),
                },
            )
        else:
            in_asset = asset
        out_unit = self.unit_a if asset.unit() == self.unit_b else self.unit_b
        out_asset = self._get_y(in_asset, out_unit, precise=precise)
        out_reserve = (
            self.reserve_b / self.asset_mulitipliers[1]
            if out_unit == self.unit_b
            else self.reserve_a / self.asset_mulitipliers[0]
        )

        out_asset.root[out_asset.unit()] = int(out_reserve - out_asset.quantity())
        if not fee_on_input:
            out_asset.root[out_asset.unit()] = int(
                out_asset.quantity() * (self.fee_basis - volume_fee) / self.fee_basis,
            )
        if precise:
            out_asset.root[out_asset.unit()] = out_asset.quantity()

        return out_asset, 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
        fee_on_input: bool = True,
    ) -> tuple[Assets, float]:
        """Calculate the amount of assets required as input to receive a given output.

        This function computes the input amount needed for a swap operation in the
        stable swap pool to achieve a desired output, taking into account the
        volume fee and precision settings.

        Args:
            asset (Assets): The desired output asset amount for the swap.
            precise (bool): If True, returns precise integer input. Defaults to True.
            fee_on_input (bool): If True, applies the fee to the calculated input.
                                        If False, applies the fee to the given output.
                                        Defaults to True.

        Returns:
            tuple[Assets, float]: A tuple containing:
                - The input asset amount required for the swap.
                - A float value (always 0 in this implementation).

        Raises:
            ValueError: If the output asset is invalid or if multiple output
            assets are provided.
        """
        volume_fee: int = 0
        if self.volume_fee is not None:
            if isinstance(self.volume_fee, (int, float)):
                volume_fee = self.volume_fee
            elif asset.unit() == self.unit_a:
                volume_fee = self.volume_fee[0]
            else:
                volume_fee = self.volume_fee[1]

        if not fee_on_input:
            out_asset = Assets(
                **{
                    asset.unit(): int(
                        asset.quantity()
                        * self.fee_basis
                        / (self.fee_basis - volume_fee),
                    ),
                },
            )
        else:
            out_asset = asset
        in_unit = self.unit_a if asset.unit() == self.unit_b else self.unit_b
        in_asset = self._get_y(out_asset, in_unit, precise=precise, get_input=True)
        in_reserve = (
            (self.reserve_b / self.asset_mulitipliers[1])
            if in_unit == self.unit_b
            else (self.reserve_a / self.asset_mulitipliers[0])
        )
        in_asset.root[in_asset.unit()] = int(in_asset.quantity() - in_reserve)
        if fee_on_input:
            in_asset.root[in_asset.unit()] = int(
                in_asset.quantity() * self.fee_basis / (self.fee_basis - volume_fee),
            )
        if precise:
            in_asset.root[in_asset.unit()] = int(in_asset.quantity())
        return in_asset, 0


class AbstractCommonStableSwapPoolState(AbstractStableSwapPoolState):
    """The common variant of StableSwap.

    This class implements the common variant of the stableswap algorithm. The main
    difference is the
    """

    def _get_ann(self) -> int:
        """The modified amp value.

        This is the ann value in the common stableswap variant.
        """
        return self.amp * N_COINS


class AbstractConstantLiquidityPoolState(AbstractPoolState):
    """Represents the state of a constant liquidity pool automated market maker (AMM).

    This class serves as a base for constant liquidity pool implementations, providing
    methods to calculate the input and output asset amounts for swaps.
    """

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Calculate the output amount for a given input in a constant liquidity pool.

        Args:
            asset (Assets): The input asset amount for the swap.
            precise (bool): If True: the output rounded to the nearest integer.

        Returns:
            tuple[Assets, float]: Tuple containing the output asset and float value.

        Raises:
            NotImplementedError: This method is not implemented in the base class.
        """
        error_msg = "CLPP amount out is not yet implemented."
        raise NotImplementedError(error_msg)

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Calculate input amount needed for desired output in constant liquidity pool.

        Args:
            asset (Assets): The desired output asset amount for the swap.
            precise (bool): If True: the output rounded to the nearest integer.

        Returns:
            tuple[Assets, float]: Tuple containing required input asset and float value.

        Raises:
            NotImplementedError: This method is not implemented in the base class.
        """
        error_msg = "CLPP amount in is not yet implemented."
        raise NotImplementedError(error_msg)
