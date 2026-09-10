"""Module providing types and state classes for AMM pools."""

import math
from typing import ClassVar

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.amm_base import AbstractPoolState
from charli3_dendrite.dexs.core.errors import InvalidPoolError

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


class AbstractConstantSumPoolState(AbstractPoolState):
    """State of a constant-sum AMM pool priced on a fixed integer value vector.

    A constant-sum pool conserves total *value* ``V = Sum(price_i * reserve_i)`` on a
    fixed integer price vector (positionally aligned to the pool assets), with NO
    amplification — it is the linear-curve sibling of
    :class:`AbstractConstantProductPoolState` (``x*y=k``) and
    :class:`AbstractStableSwapPoolState` (the amplified Curve invariant), and is NOT a
    stable-swap variant. Within a routable 2-asset ``(i, j)`` leg a swap of ``dx`` of
    asset ``i`` preserves ``V`` minus the floored LP fee: the pool retains
    ``floor(dx * p_i * fee_num / fee_den)`` of value and pays the remainder out in
    asset ``j`` at the fixed price ratio ``p_i : p_j``. Output is bounded by the
    out-side reserve ``reserve_j`` — a constant-sum leg cannot pay out more of an
    asset than it holds.

    The returned output is the unique maximum that satisfies the on-chain constant-sum
    swap-step relation (the value increase ``v_increase`` is pinned to exactly
    ``floor(input_value * fee_num / fee_den)``): a larger output over-pays (fails the
    tightness bound) and a smaller output over-charges (fails the achievable bound).

    Concrete constant-sum DEXs supply only the per-leg price weights and fee via the
    two hooks below (:meth:`_cs_price_pair` and :meth:`_cs_fee`), mirroring how
    :class:`AbstractStableSwapPoolState` exposes ``amp``/``_get_ann`` and
    :class:`AbstractConstantLiquidityPoolState` exposes ``_sqrt_price_bounds`` — the
    standard ``reserve_a`` / ``reserve_b`` / ``unit_a`` / ``unit_b`` interface drives
    the math.
    """

    fee_basis: int = 10000

    # -- per-DEX hooks (the analogue of the stable base's amp/_get_ann) --------
    def _cs_price_pair(self) -> tuple[int, int]:
        """Integer price weights ``(price_a, price_b)`` aligned to ``(unit_a, unit_b)``.

        The constant-sum value vector restricted to this 2-asset leg. Defaults to an
        equal-price ``(1, 1)`` leg (the common stable-pair case); override when a leg
        carries non-unit integer price weights.
        """
        return (1, 1)

    def _cs_fee(self) -> tuple[int, int]:
        """LP swap fee as an exact integer ratio ``(fee_num, fee_den)``.

        Defaults to ``volume_fee / fee_basis``; override to supply the on-chain
        ``fee_num`` / ``fee_den`` directly (the on-chain check floors
        ``input_value * fee_num / fee_den``).
        """
        fee = self.volume_fee
        if fee is None:
            return (0, self.fee_basis)
        if isinstance(fee, (list, tuple)):
            return (int(fee[0]), self.fee_basis)
        return (int(fee), self.fee_basis)

    # -- generic constant-sum value-conservation math -------------------------
    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Output amount + price impact for an input ``asset`` (capped at the reserve).

        Value-conservation forward quote: the pool retains the floored fee on the input
        value and pays out the remaining value at the fixed price ratio, capped at the
        out-side reserve. This is the unique maximum output that satisfies the on-chain
        constant-sum step relation.

        Args:
            asset (Assets): The input asset amount for the swap.
            precise (bool): Accepted for interface parity; the output is always the
                integer floor.

        Returns:
            tuple[Assets, float]: The output asset and the price-impact ratio.
        """
        if len(asset) != 1 or asset.unit() not in (self.unit_a, self.unit_b):
            error_msg = f"Invalid input asset for pool: {asset}"
            raise ValueError(error_msg)

        price_a, price_b = self._cs_price_pair()
        fee_num, fee_den = self._cs_fee()
        if asset.unit() == self.unit_a:
            price_in, price_out, out_real, out_unit = (
                price_a,
                price_b,
                self.reserve_b,
                self.unit_b,
            )
        else:
            price_in, price_out, out_real, out_unit = (
                price_b,
                price_a,
                self.reserve_a,
                self.unit_a,
            )

        amount_in = asset.quantity()
        input_value = amount_in * price_in
        # Floor the fee — the pool wins the dust, exactly as the on-chain check pins
        # v_increase = floor(input_value * fee_num / fee_den).
        fee_value = input_value * fee_num // fee_den
        out_value = input_value - fee_value
        expected_out = min(out_value // price_out, out_real)
        out_assets = Assets(**{out_unit: expected_out})
        if not precise:
            out_assets.root[out_unit] = expected_out

        if amount_in == 0 or expected_out == 0:
            return out_assets, 0.0
        # Spot (out per in) is the fixed ratio price_in/price_out; on an uncapped leg
        # the only wedge is the fee, so the impact equals the fee fraction (it grows
        # once the output saturates the reserve cap).
        effective = (expected_out * price_out) / (amount_in * price_in)
        return out_assets, 1.0 - effective

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Minimum input + price impact to obtain a desired output ``asset``.

        Algebraic inverse of :meth:`get_amount_out`: the least input whose post-fee
        value covers the requested output value at the fixed price ratio.

        Args:
            asset (Assets): The desired output asset amount for the swap.
            precise (bool): Accepted for interface parity; the input is the integer
                ceiling (the minimal whole-unit input).

        Returns:
            tuple[Assets, float]: The required input asset and the price-impact ratio.

        Raises:
            InvalidPoolError: If the desired output exceeds the leg's reserve.
        """
        if len(asset) != 1 or asset.unit() not in (self.unit_a, self.unit_b):
            error_msg = f"Invalid output asset for pool: {asset}"
            raise ValueError(error_msg)
        desired_out = asset.quantity()
        if desired_out <= 0:
            error_msg = "desired output must be positive"
            raise ValueError(error_msg)

        price_a, price_b = self._cs_price_pair()
        fee_num, fee_den = self._cs_fee()
        if asset.unit() == self.unit_b:
            out_real, price_in, price_out, in_unit = (
                self.reserve_b,
                price_a,
                price_b,
                self.unit_a,
            )
        else:
            out_real, price_in, price_out, in_unit = (
                self.reserve_a,
                price_b,
                price_a,
                self.unit_b,
            )
        if desired_out >= out_real:
            error_msg = (
                f"Desired output {desired_out} exceeds available reserve {out_real}"
            )
            raise InvalidPoolError(error_msg)

        out_value = desired_out * price_out
        net = fee_den - fee_num

        def _produced(amount: int) -> int:
            value = amount * price_in
            return (value - value * fee_num // fee_den) // price_out

        # Closed-form ceil is a valid upper bound (it ignores the fee-flooring that
        # makes small inputs cheaper); flooring can move the true minimum down a few
        # units, so search down to the minimum, then back up to guarantee coverage.
        amount_in = max(1, -(-(out_value * fee_den) // (price_in * net)))
        while amount_in > 1 and _produced(amount_in - 1) >= desired_out:
            amount_in -= 1
        while _produced(amount_in) < desired_out:
            amount_in += 1
        in_assets = Assets(**{in_unit: amount_in})
        if not precise:
            in_assets.root[in_unit] = amount_in

        if amount_in == 0:
            return in_assets, 0.0
        effective = (desired_out * price_out) / (amount_in * price_in)
        return in_assets, 1.0 - effective


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
        volume_fee: int | float = 0
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
        volume_fee: int | float = 0
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
    """State of a single-band concentrated-liquidity ("constant liquidity") AMM pool.

    A concentrated-liquidity position holds liquidity only within one
    ``[sqrt_lower, sqrt_upper]`` price band. Inside the band the curve is a constant
    product on VIRTUAL reserves ``(a_v, b_v)`` extrapolated from the band bounds; a
    swap large enough to push the price past the band is capped at the band's
    available output (a single UTxO cannot fill beyond its range). This base
    implements that math against the standard ``reserve_a``/``reserve_b``/``unit_a``/
    ``unit_b``/``volume_fee`` interface — exactly as
    :class:`AbstractConstantProductPoolState` does for plain CPP and
    :class:`AbstractStableSwapPoolState` does for stable swaps. Concrete CLMM DEXs
    (Dano, Sundae V4) supply only the per-band datum specifics via the two hooks
    below (``_sqrt_price_bounds`` and, where they net fees/carve-outs, the
    ``reserve_a``/``reserve_b`` overrides), mirroring how the stable base exposes
    ``amp``/``_get_ann``.
    """

    fee_basis: int = 10000

    # ── per-DEX hooks (the analogue of the stable base's amp/_get_ann) ────────
    def _sqrt_price_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Return ``((lower_num, lower_den), (upper_num, upper_den))``.

        The square-root price band bounds as exact integer ratios, read from the
        pool datum: ``lower`` is ``sqrt(P_a)``, ``upper`` is ``sqrt(P_b)``.
        """
        raise NotImplementedError

    @property
    def _lp_fee_rate(self) -> int:
        """LP fee rate in ``fee_basis`` units — the only fee affecting swap output.

        Defaults to ``volume_fee`` (which surfaces ``fee``); a concentrated-liquidity
        position carries one symmetric LP fee. Override if a DEX encodes a per-side
        fee list. Any platform/protocol fee skims the LP's cut, not the swapper's
        output, so it is deliberately excluded here.
        """
        fee = self.volume_fee
        if fee is None:
            return 0
        if isinstance(fee, (list, tuple)):
            return int(fee[0])
        return int(fee)

    # ── generic single-band CLMM math ────────────────────────────────────────
    def virtual_reserves(self) -> tuple[int, int]:
        """Virtual reserves ``(a_v, b_v)`` of the active band (aligned to a, b).

        The constant-product reserves the band's liquidity ``L`` is equivalent to
        within ``[sqrt_lower, sqrt_upper]`` (the Uniswap-V3 single-band identity
        ``a_v = a + L/sqrt(P_b)``, ``b_v = b + L*sqrt(P_a)``), solved in exact integer
        arithmetic. The ``a_v / b_v`` ratio is the band's marginal price.
        """
        a, b = self.reserve_a, self.reserve_b
        (pa_n, pa_d), (pb_n, pb_d) = self._sqrt_price_bounds()
        den_ab = pa_d * pb_d
        num_ab = pa_n * pb_n
        diff = b * den_ab - a * num_ab
        big = math.isqrt(diff * diff + 4 * a * b * pa_d * pa_d * pb_n * pb_n)
        liq_num = b * den_ab + a * num_ab + big
        liq_den = 2 * (pb_n * pa_d - pb_d * pa_n)
        # ceilDiv for each virtual-reserve offset (``-(-n // d)``).
        a_v = -(-(liq_num * pb_d) // (liq_den * pb_n)) + a
        b_v = -(-(liq_num * pa_n) // (liq_den * pa_d)) + b
        return a_v, b_v

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Output amount + price impact for an input ``asset`` (capped at the band).

        Args:
            asset (Assets): The input asset amount for the swap.
            precise (bool): Accepted for interface parity; the output is always the
                integer floor (a single band trades whole units).

        Returns:
            tuple[Assets, float]: The output asset and the price-impact ratio.
        """
        if len(asset) != 1 or asset.unit() not in (self.unit_a, self.unit_b):
            error_msg = f"Invalid input asset for pool: {asset}"
            raise ValueError(error_msg)

        a_v, b_v = self.virtual_reserves()
        if asset.unit() == self.unit_a:
            in_v, out_v, out_real, out_unit = a_v, b_v, self.reserve_b, self.unit_b
        else:
            in_v, out_v, out_real, out_unit = b_v, a_v, self.reserve_a, self.unit_a

        amount_in = asset.quantity()
        off_fee = self.fee_basis - self._lp_fee_rate
        denominator = in_v * self.fee_basis + amount_in * off_fee
        if denominator <= 0:
            # Degenerate band (virtual in-reserve 0) probed with zero input: no
            # output, no division. Guarding above the floor-division lets a
            # parked-band probe return (0, 0.0) rather than ZeroDivisionError.
            return Assets(**{out_unit: 0}), 0.0
        numerator = out_v * denominator - in_v * out_v * self.fee_basis
        # Capacity cap: one band holds only ``out_real`` of the output token; a
        # larger swap would push price past the band, which this UTxO cannot fill.
        # Cap the fill (order-book convention) rather than raise — callers route
        # the remainder to other bands. A band parked at its edge has
        # ``out_real == 0`` and correctly yields zero output.
        expected_out = min(numerator // denominator, out_real)
        out_assets = Assets(**{out_unit: expected_out})
        if not precise:
            out_assets.root[out_unit] = expected_out

        if amount_in == 0 or expected_out == 0:
            return out_assets, 0.0
        spot = out_v / in_v
        effective = expected_out / amount_in
        return out_assets, 1.0 - (effective / spot)

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Minimum input + price impact to obtain a desired output ``asset``.

        Algebraic inverse of :meth:`get_amount_out` in-band:
        ``in_min = ceil(in_v * basis * out / ((out_v - out) * offFee))``.

        Args:
            asset (Assets): The desired output asset amount for the swap.
            precise (bool): Accepted for interface parity; the input is always the
                integer ceiling (the minimal whole-unit input).

        Returns:
            tuple[Assets, float]: The required input asset and the price-impact ratio.

        Raises:
            InvalidPoolError: If the desired output exceeds the band's available
                reserve (it cannot be filled from this UTxO).
        """
        if len(asset) != 1 or asset.unit() not in (self.unit_a, self.unit_b):
            error_msg = f"Invalid output asset for pool: {asset}"
            raise ValueError(error_msg)
        desired_out = asset.quantity()
        if desired_out <= 0:
            error_msg = "desired output must be positive"
            raise ValueError(error_msg)

        a_v, b_v = self.virtual_reserves()
        off_fee = self.fee_basis - self._lp_fee_rate
        if asset.unit() == self.unit_b:
            out_real, in_v, out_v, in_unit = self.reserve_b, a_v, b_v, self.unit_a
        else:
            out_real, in_v, out_v, in_unit = self.reserve_a, b_v, a_v, self.unit_b
        if desired_out >= out_real:
            error_msg = (
                f"Desired output {desired_out} exceeds available reserve {out_real}"
            )
            raise InvalidPoolError(error_msg)
        amount_in = -(
            -(in_v * self.fee_basis * desired_out) // ((out_v - desired_out) * off_fee)
        )
        in_assets = Assets(**{in_unit: amount_in})
        if not precise:
            in_assets.root[in_unit] = amount_in

        if amount_in == 0:
            return in_assets, 0.0
        spot = out_v / in_v
        effective = desired_out / amount_in
        return in_assets, 1.0 - (effective / spot)
