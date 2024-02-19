from cardex.dataclasses.models import Assets
from cardex.dexs.amm_base import AbstractPoolState


class AbstractConstantProductPoolState(AbstractPoolState):
    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the output asset amount given an input asset amount.

        Args:
            asset: An asset with a defined quantity.

        Returns:
            A tuple where the first value is the estimated asset returned from the swap
                and the second value is the price impact ratio.
        """
        assert len(asset) == 1, "Asset should only have one token."
        assert asset.unit() in [
            self.unit_a,
            self.unit_b,
        ], f"Asset {asset.unit} is invalid for pool {self.unit_a}-{self.unit_b}"

        if asset.unit() == self.unit_a:
            reserve_in, reserve_out = self.reserve_a, self.reserve_b
            unit_out = self.unit_b
        else:
            reserve_in, reserve_out = self.reserve_b, self.reserve_a
            unit_out = self.unit_a

        # Calculate the amount out
        fee_modifier = 10000 - self.volume_fee
        numerator: int = asset.quantity() * fee_modifier * reserve_out
        denominator: int = asset.quantity() * fee_modifier + reserve_in * 10000
        amount_out = Assets(**{unit_out: numerator // denominator})
        if not precise:
            amount_out.root[unit_out] = numerator / denominator

        if amount_out.quantity() == 0:
            return amount_out, 0

        # Calculate the price impact
        price_numerator: int = (
            reserve_out * asset.quantity() * denominator * fee_modifier
            - numerator * reserve_in * 10000
        )
        price_denominator: int = reserve_out * asset.quantity() * denominator * 10000
        price_impact: float = price_numerator / price_denominator

        return amount_out, price_impact

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Get the input asset amount given a desired output asset amount.

        Args:
            asset: An asset with a defined quantity.

        Returns:
            The estimated asset needed for input in the swap.
        """
        assert len(asset) == 1, "Asset should only have one token."
        assert asset.unit() in [
            self.unit_a,
            self.unit_b,
        ], f"Asset {asset.unit} is invalid for pool {self.unit_a}-{self.unit_b}"
        if asset.unit() == self.unit_b:
            reserve_in, reserve_out = self.reserve_a, self.reserve_b
            unit_out = self.unit_a
        else:
            reserve_in, reserve_out = self.reserve_b, self.reserve_a
            unit_out = self.unit_b

        # Estimate the required input
        fee_modifier = 10000 - self.volume_fee
        numerator: int = asset.quantity() * 10000 * reserve_in
        denominator: int = (reserve_out - asset.quantity()) * fee_modifier
        amount_in = Assets(**{unit_out: numerator // denominator})
        if not precise:
            amount_in.root[unit_out] = numerator / denominator

        # Estimate the price impact
        price_numerator: int = (
            reserve_out * numerator * fee_modifier
            - asset.quantity() * denominator * reserve_in * 10000
        )
        price_denominator: int = reserve_out * numerator * 10000
        price_impact: float = price_numerator / price_denominator

        return amount_in, price_impact


class AbstractStableSwapPoolState(AbstractPoolState):
    @property
    def amp(cls) -> Assets:
        return 75

    def _get_D(self) -> float:
        """Regression to learn the stability constant."""
        # TODO: Expand this to operate on pools with more than one stable
        N_COINS = 2
        Ann = self.amp * N_COINS**N_COINS
        S = self.reserve_a + self.reserve_b
        if S == 0:
            return 0

        # Iterate until the change in value is <1 unit.
        D = S
        for i in range(256):
            D_P = D**3 / (N_COINS**N_COINS * self.reserve_a * self.reserve_b)
            D_prev = D
            D = D * (Ann * S + D_P * N_COINS) / ((Ann - 1) * D + (N_COINS + 1) * D_P)

            if abs(D - D_prev) < 1:
                break

        return D

    def _get_y(self, in_assets: Assets, out_unit: str, precise: bool = True):
        """Calculate the output amount using a regression."""
        N_COINS = 2
        Ann = self.amp * N_COINS**N_COINS
        D = self._get_D()

        # Make sure only one input supplied
        if len(in_assets) > 1:
            raise ValueError("Only one input asset allowed.")
        elif in_assets.unit() not in [self.unit_a, self.unit_b]:
            raise ValueError("Invalid input token.")
        elif out_unit not in [self.unit_a, self.unit_b]:
            raise ValueError("Invalid output token.")

        in_quantity = in_assets.quantity() * (10000 - self.volume_fee) / 10000
        if in_assets.unit() == self.unit_a:
            in_reserve = self.reserve_a + in_quantity
        else:
            in_reserve = self.reserve_b + in_quantity

        S = in_reserve
        c = D**3 / (N_COINS**2 * Ann * in_reserve)
        b = S + D / Ann
        out_prev = 0
        out = D

        for i in range(256):
            out_prev = out
            out = (out**2 + c) / (2 * out + b - D)

            if abs(out - out_prev) < 1:
                break

        out_assets = Assets(**{out_unit: int(out)})
        if not precise:
            out_assets.root[out_unit] = out

        return out_assets

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        out_unit = self.unit_a if asset.unit() == self.unit_b else self.unit_b
        out_asset = self._get_y(asset, out_unit, precise=precise)
        out_reserve = self.reserve_b if out_unit == self.unit_b else self.reserve_a
        if precise:
            out_asset.root[out_asset.unit()] = int(out_reserve - out_asset.quantity())
        else:
            out_asset.root[out_asset.unit()] = out_reserve - out_asset.quantity()
        return out_asset, 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        in_unit = self.unit_a if asset.unit() == self.unit_b else self.unit_b
        in_asset = self._get_y(asset, in_unit, precise=precise)
        in_reserve = self.reserve_b if in_unit == self.unit_b else self.reserve_a
        if precise:
            in_asset.root[in_asset.unit()] = int(in_asset.quantity() - in_reserve)
        else:
            in_asset.root[in_asset.unit()] = in_asset.quantity() - in_reserve
        return in_asset, 0


class AbstractConstantLiquidityPoolState(AbstractPoolState):
    def get_amount_out(self, asset: Assets) -> tuple[Assets, float]:
        raise NotImplementedError("CLPP amount out is not yet implemented.")
        return out_asset, 0

    def get_amount_in(self, asset: Assets) -> tuple[Assets, float]:
        raise NotImplementedError("CLPP amount out is not yet implemented.")
        return out_asset, 0
