"""Dano Concentrated Liquidity (CLMM) DEX module.

Skeleton port of the Dano CLMM SDK (TypeScript) to Dendrite. Off-chain
swap construction is intentionally left unimplemented — Dano executes
swaps directly against the pool with a custom redeemer rather than via a
batcher/order-datum flow, so `swap_utxo` does not map cleanly onto the
default `AbstractPoolState` contract.

Reference SDK: D:/teko_source/CLMM/clmm-sdk-init-sdk/src/
  - datum.ts            -> DanoPoolDatum
  - utils.ts            -> get_amount_out / get_amount_in math
  - constants.ts        -> pool script hash, protocol config out-ref
  - concentratedPool.ts -> ConcentratedPool field layout
"""

from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal
from math import isqrt
from typing import Any
from typing import ClassVar

from pycardano import Address
from pycardano import IndefiniteList
from pycardano import MultiAsset
from pycardano import PlutusData
from pycardano import PlutusV2Script
from pycardano import RawCBOR
from pycardano import Redeemer
from pycardano import RedeemerTag
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value

from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_base import AbstractPoolState
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.utility import asset_to_value


# Mainnet — see clmm-sdk-init-sdk/src/constants.ts
DANO_POOL_SCRIPT_HASH_MAINNET = (
    "d8b69fc53637bcfadbc4469083f706bc293f4d9d2296646c5ca167bb"
)
DANO_POOL_SCRIPT_HASH_PREPROD = (
    "04041c3c6ba87b33f2c9eb7f7dbeae3b26003c3e199d438bb99932a2"
)

FEE_BASIS = 10_000
# ADA carve-out subtracted from active reserve when tokenX is ADA
# (utils.ts:28: `3_000_000n + totalSwapFee`)
ADA_MIN_UTXO = 3_000_000

SWAP_ACTION = 3  # see 01-validator.md section Redeemers

# Pool script address on mainnet (addr1x8vtd…vrg4) — the payment part is
# the pool validator and the staking part is a separate delegation script.
DANO_POOL_ADDRESS_MAINNET = (
    "addr1x8vtd879xcmme7kmc3rfpqlhq67zj06dn53fvervtjsk0w"
    "7dwgsd23ac468cjj8rcnyuc3s72rtupu6j9dw0xpw83exsufvrg4"
)
# Reward address used for the withdraw-zero trick. Its script hash equals
# the pool payment-script hash, so the same reference script serves both
# the spend and reward redeemer.
DANO_POOL_REWARD_ADDRESS_MAINNET = (
    "stake178vtd879xcmme7kmc3rfpqlhq67zj06dn53fvervtjsk0wczh2pdw"
)
# Reference UTxOs — mainnet (see CLMM/clmm-sdk-init-sdk/src/constants.ts)
POOL_SCRIPT_OUT_REF_MAINNET = (
    "64d111b957e7d7848ffdde5149aa77fa4090a7fa1ad0ac108067900614848501#0"
)
PROTOCOL_CONFIG_OUT_REF_MAINNET = (
    "2cafd7c92f7093e5229af274be83dea660b0590b4174bbed79ba662b44fbd1ee#0"
)


def _bigint_to_bytes_padded(n: int, length: int) -> bytes:
    """Pad a signed integer to `length` bytes big-endian.

    Matches the two's-complement encoding in redeemer.ts:bigintToBytesPadded:
    negative numbers are represented as `n + 2^(length*8)`.
    """
    unsigned = n if n >= 0 else n + (1 << (length * 8))
    return unsigned.to_bytes(length, "big")


def build_swap_redeemer_bytes(
    delta_amount: int,
    pool_in_idx: int,
    pool_out_idx: int,
    first_byte: int,
) -> bytes:
    """Pack the single-pool swap redeemer bytestring (36 bytes)."""
    return build_batch_swap_redeemer_bytes(
        first_byte=first_byte,
        entries=[(pool_in_idx, pool_out_idx, delta_amount)],
    )


def build_batch_swap_redeemer_bytes(
    first_byte: int,
    entries: list[tuple[int, int, int]],
) -> bytes:
    """Pack a multi-pool swap redeemer.

    Layout (see 01-validator.md § Swap):
        [0]    first byte — `pool_in_idx` for Spend, `protocol_config_idx`
               for Withdraw (the two purposes use different semantics for
               this byte but the same format otherwise).
        [1]    action type = SWAP (3)
        [2..]  repeated per-pool entry of 34 bytes each:
                  (pool_in_idx: u8, pool_out_idx: u8, delta_amount: i256)
    """
    out = first_byte.to_bytes(1, "big") + SWAP_ACTION.to_bytes(1, "big")
    for pool_in_idx, pool_out_idx, delta_amount in entries:
        out += (
            pool_in_idx.to_bytes(1, "big")
            + pool_out_idx.to_bytes(1, "big")
            + _bigint_to_bytes_padded(delta_amount, 32)
        )
    return out


def parse_swap_redeemer_bytes(
    payload: bytes,
) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Inverse of build_batch_swap_redeemer_bytes.

    Returns ``(first_byte, action, entries)`` where each entry is
    ``(pool_in_idx, pool_out_idx, delta_amount)`` with delta as a signed int.
    """
    first_byte = payload[0]
    action = payload[1]
    entries: list[tuple[int, int, int]] = []
    pos = 2
    while pos < len(payload):
        pool_in_idx = payload[pos]
        pool_out_idx = payload[pos + 1]
        amount_bytes = payload[pos + 2 : pos + 34]
        amount = int.from_bytes(amount_bytes, "big", signed=False)
        if amount >= (1 << 255):
            amount -= 1 << 256
        entries.append((pool_in_idx, pool_out_idx, amount))
        pos += 34
    return first_byte, action, entries


@dataclass
class Ratio(PlutusData):
    """A rational number stored on-chain as (num, den)."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


def _bytes_pair_to_unit(pair: IndefiniteList) -> str:
    """Convert on-chain `[policy, asset_name]` to a Dendrite asset unit."""
    items = list(pair) if isinstance(pair, IndefiniteList) else list(pair)
    policy_hex = items[0].hex()
    name_hex = items[1].hex()
    return "lovelace" if policy_hex == "" and name_hex == "" else policy_hex + name_hex


@dataclass
class DanoPoolDatum(PoolDatum):
    """Dano CLMM pool datum.

    Field order MUST match the on-chain Constr layout from
    clmm-sdk-init-sdk/src/datum.ts: transformPoolDatum().

    Note: token_x / token_y are bare 2-element arrays of `[policy, name]`,
    NOT Constr-wrapped AssetClass values. They are typed as IndefiniteList
    so pycardano emits CBOR with the `9f...ff` indefinite-length encoding
    used by the on-chain validator.
    """

    CONSTR_ID = 0

    token_x: IndefiniteList
    token_y: IndefiniteList
    lp_fee_rate: int
    platform_fee_x: int
    platform_fee_y: int
    total_swap_fee: int
    sqrt_lower_price: Ratio
    sqrt_upper_price: Ratio
    min_x_change: int
    min_y_change: int
    circulating_lp_token: int
    last_withdraw_epoch: int

    @property
    def unit_x(self) -> str:
        return _bytes_pair_to_unit(self.token_x)

    @property
    def unit_y(self) -> str:
        return _bytes_pair_to_unit(self.token_y)

    def pool_pair(self) -> Assets | None:
        return Assets(root={self.unit_x: 0, self.unit_y: 0})


class DanoCLMMState(AbstractPoolState):
    """State of a single Dano concentrated-liquidity pool."""

    fee: int = 0  # populated from datum.lp_fee_rate in post_init

    # Dano pools have no batcher and no user-paid deposit:
    # the swap redeemer mutates the pool UTxO directly.
    _batcher: ClassVar[Assets] = Assets(lovelace=0)
    _deposit: ClassVar[Assets] = Assets(lovelace=0)

    # Platform fee rate lives in a SEPARATE protocol-config UTxO
    # (constants.ts: PROTOCOL_CONFIG_OUT_REF_*). It is required for
    # accurate swap math but is NOT in the pool datum. Inject it before
    # calling get_amount_*; otherwise the LP fee is applied without the
    # platform-fee skim.
    platform_fee_rate: int = 0

    _stake_address: ClassVar[Address] = Address(
        payment_part=ScriptHash(bytes.fromhex(DANO_POOL_SCRIPT_HASH_MAINNET)),
    )

    @classmethod
    def dex(cls) -> str:
        return "Dano"

    @classmethod
    def order_selector(cls) -> list[str]:
        # No off-chain order address: swaps are direct.
        return [cls._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        return PoolSelector(
            addresses=[cls._stake_address.encode()],
            assets=cls.dex_policy(),
        )

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        # Dano mints the validity NFT from the pool validator itself,
        # so the policy id == pool script hash. The 32-byte asset name is
        # a per-pool unique identifier.
        return [DANO_POOL_SCRIPT_HASH_MAINNET]

    @classmethod
    def pool_policy(cls) -> list[str] | None:
        # The validity NFT is already extracted by dex_policy(); Dano
        # uses a single NFT for both roles, so don't double-count.
        return None

    @classmethod
    def default_script_class(cls) -> type[PlutusV2Script]:
        return PlutusV2Script

    @property
    def swap_forward(self) -> bool:
        return False

    @property
    def stake_address(self) -> Address:
        return self._stake_address

    @classmethod
    def pool_datum_class(cls) -> type[DanoPoolDatum]:
        return DanoPoolDatum

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        # Dano has no order datum. Raising keeps callers honest until a
        # bespoke swap-redeemer flow is implemented.
        raise NotImplementedError("Dano CLMM does not use order datums.")

    @property
    def pool_id(self) -> str:
        if self.dex_nft is None:
            raise InvalidPoolError("Dano pool is missing its validity NFT.")
        return self.dex_nft.unit()

    # --- reserves ----------------------------------------------------------

    @property
    def _datum(self) -> DanoPoolDatum:
        return self.pool_datum  # type: ignore[return-value]

    # NOTE: Dendrite's `Assets` collection sorts units alphabetically,
    # so `assets.quantity(0)` is NOT necessarily token X. We always look
    # up reserves by the datum-declared unit_x / unit_y instead.

    @property
    def raw_x(self) -> int:
        return self.assets[self._datum.unit_x]

    @property
    def raw_y(self) -> int:
        return self.assets[self._datum.unit_y]

    @property
    def reserve_a(self) -> int:
        """Active reserve of tokenX, net of platform fees and ADA carve-out."""
        d = self._datum
        excluded_ada = (
            ADA_MIN_UTXO + d.total_swap_fee if d.unit_x == "lovelace" else 0
        )
        return self.raw_x - d.platform_fee_x - excluded_ada

    @property
    def reserve_b(self) -> int:
        """Active reserve of tokenY, net of platform fees."""
        return self.raw_y - self._datum.platform_fee_y

    # --- math (port of utils.ts:calculateConcentratedPoolSwap) -------------

    def _virtual_reserves(self) -> tuple[int, int]:
        """Compute virtual reserves (xV, yV) used by the CLMM invariant.

        Mirrors `calcLiquidity` + the xV/yV step in utils.ts.
        """
        d = self._datum
        x = self.reserve_a
        y = self.reserve_b
        pa_n, pa_d = d.sqrt_lower_price.numerator, d.sqrt_lower_price.denominator
        pb_n, pb_d = d.sqrt_upper_price.numerator, d.sqrt_upper_price.denominator

        den_a_den_b = pa_d * pb_d
        num_a_num_b = pa_n * pb_n

        diff = y * den_a_den_b - x * num_a_num_b
        big = isqrt(diff * diff + 4 * x * y * pa_d * pa_d * pb_n * pb_n)

        liq_num = y * den_a_den_b + x * num_a_num_b + big
        liq_den = 2 * (pb_n * pa_d - pb_d * pa_n)

        # ceilDiv equivalents
        x_v = -(-(liq_num * pb_d) // (liq_den * pb_n)) + x
        y_v = -(-(liq_num * pa_n) // (liq_den * pa_d)) + y
        return x_v, y_v

    def _swap_out(self, amount_in: int, in_virtual: int, out_virtual: int,
                  out_real: int) -> tuple[int, int]:
        """Port of utils.ts:getPoolChange. Returns (out_amount, platform_fee)."""
        lp_fee = (amount_in * self._datum.lp_fee_rate) // FEE_BASIS
        platform_fee = (lp_fee * self.platform_fee_rate) // FEE_BASIS
        off_fee = FEE_BASIS - self._datum.lp_fee_rate

        denominator = in_virtual * FEE_BASIS + amount_in * off_fee
        numerator = out_virtual * denominator - in_virtual * out_virtual * FEE_BASIS
        expected_out = numerator // denominator

        if expected_out > out_real:
            raise InvalidPoolError("Swap exceeds available pool reserves.")

        return expected_out, platform_fee

    def get_amount_out(self, asset: Assets) -> tuple[Assets, float]:
        d = self._datum
        if len(asset) != 1 or asset.unit() not in (d.unit_x, d.unit_y):
            raise ValueError(f"Invalid input asset for pool: {asset}")

        x_v, y_v = self._virtual_reserves()
        if asset.unit() == d.unit_x:
            in_v, out_v, out_real, out_unit = x_v, y_v, self.reserve_b, d.unit_y
        else:
            in_v, out_v, out_real, out_unit = y_v, x_v, self.reserve_a, d.unit_x

        out_qty, _ = self._swap_out(asset.quantity(), in_v, out_v, out_real)
        out_assets = Assets(**{out_unit: out_qty})

        # Price impact: 1 - (effective_price / spot_price)
        # Approximated against virtual reserves.
        if asset.quantity() == 0 or out_qty == 0:
            return out_assets, 0.0
        spot = out_v / in_v
        effective = out_qty / asset.quantity()
        price_impact = 1.0 - (effective / spot)
        return out_assets, price_impact

    def get_amount_in(self, asset: Assets) -> tuple[Assets, float]:
        # TODO: invert _swap_out. The CLMM invariant is closed-form, so this
        # is solvable algebraically — port it once get_amount_out is
        # validated against a live preprod pool.
        raise NotImplementedError

    # --- spec-driven helpers ----------------------------------------------

    def active_liquidity(self, staking_reward: int = 0) -> tuple[int, int]:
        """Return (poolinLPX, poolinLPY) per 00-biz-spec.md § Common Temp Vars.

        ``staking_reward`` is nonzero only when ADA rewards are being
        withdrawn into the pool as part of the same tx (see docs/05-04-swap.md
        "If token_x is ADA AND curEpoch > last_withdraw_epoch").
        """
        d = self._datum
        x_raw = self.raw_x + staking_reward
        y_raw = self.raw_y
        if d.unit_x == "lovelace":
            pool_in_lp_x = (
                x_raw - d.platform_fee_x - d.total_swap_fee - ADA_MIN_UTXO
            )
        else:
            pool_in_lp_x = x_raw - d.platform_fee_x
        pool_in_lp_y = y_raw - d.platform_fee_y
        return pool_in_lp_x, pool_in_lp_y

    def compute_pool_change(
        self,
        delta_amount: int,
        staking_reward: int = 0,
    ) -> tuple[int, int]:
        """Return signed (poolChangeX, poolChangeY) per spec § Redeemer: Swap.

        Positive ``delta_amount`` => X→Y swap of ``delta_amount`` units of X;
        negative => Y→X swap of ``|delta_amount|`` units of Y.
        """
        d = self._datum
        pool_in_lp_x, pool_in_lp_y = self.active_liquidity(staking_reward)

        pa_n, pa_d = d.sqrt_lower_price.numerator, d.sqrt_lower_price.denominator
        pb_n, pb_d = d.sqrt_upper_price.numerator, d.sqrt_upper_price.denominator

        den_ab = pa_d * pb_d
        num_ab = pa_n * pb_n

        diff = pool_in_lp_y * den_ab - pool_in_lp_x * num_ab
        big = isqrt(
            diff * diff + 4 * pool_in_lp_x * pool_in_lp_y * pa_d * pa_d * pb_n * pb_n
        )
        liq_num = pool_in_lp_y * den_ab + pool_in_lp_x * num_ab + big
        liq_den = 2 * (pb_n * pa_d - pb_d * pa_n)

        x_v = -(-(liq_num * pb_d) // (liq_den * pb_n)) + pool_in_lp_x
        y_v = -(-(liq_num * pa_n) // (liq_den * pa_d)) + pool_in_lp_y

        off_fee = FEE_BASIS - d.lp_fee_rate  # basis minus fee rate

        if delta_amount > 0:
            # poolChangeY = max(ceil(Xv*Yv / (Xv + poolChangeX*offFee) - Yv), -poolinLPY)
            # rewritten in integer arithmetic
            numerator = x_v * y_v * FEE_BASIS
            denom = x_v * FEE_BASIS + delta_amount * off_fee
            # ceil(numerator/denom) - y_v
            y_new_ceil = -(-numerator // denom)
            pool_change_y = max(y_new_ceil - y_v, -pool_in_lp_y)
            return delta_amount, pool_change_y

        if delta_amount < 0:
            amount_y_in = -delta_amount
            numerator = x_v * y_v * FEE_BASIS
            denom = y_v * FEE_BASIS + amount_y_in * off_fee
            x_new_ceil = -(-numerator // denom)
            pool_change_x = max(x_new_ceil - x_v, -pool_in_lp_x)
            return pool_change_x, amount_y_in

        raise ValueError("delta_amount must be non-zero")

    def compute_new_datum(
        self,
        delta_amount: int,
        swap_fee: int,
        cur_epoch: int,
    ) -> DanoPoolDatum:
        """Build the new pool datum per spec § Redeemer: Swap § 3.2.

        Platform-fee accrual uses the redeemer ``delta_amount`` (verified
        against mainnet tx 716ce79…). ``swap_fee`` is the protocol-config
        constant added to ``total_swap_fee``.
        """
        d = self._datum
        new_x_fee = d.platform_fee_x
        new_y_fee = d.platform_fee_y

        if delta_amount > 0:
            lp_fee = (delta_amount * d.lp_fee_rate) // FEE_BASIS
            new_x_fee += (lp_fee * self.platform_fee_rate) // FEE_BASIS
        else:
            amt = -delta_amount
            lp_fee = (amt * d.lp_fee_rate) // FEE_BASIS
            new_y_fee += (lp_fee * self.platform_fee_rate) // FEE_BASIS

        return replace(
            d,
            platform_fee_x=new_x_fee,
            platform_fee_y=new_y_fee,
            total_swap_fee=d.total_swap_fee + swap_fee,
            last_withdraw_epoch=cur_epoch,
        )

    # --- swap construction -------------------------------------------------

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
        raise NotImplementedError(
            "Dano swaps are direct-spend; use build_swap_tx() instead of "
            "the batcher-oriented swap_utxo() contract.",
        )

    def build_swap_tx(
        self,
        context: Any,  # pycardano ChainContext — type-loose to avoid import cycles
        address_source: Address,
        delta_amount: int,
        swap_fee: int,
        cur_epoch: int,
        pool_utxo: UTxO,
        pool_script_ref_utxo: UTxO,
        protocol_config_utxo: UTxO,
        user_inputs: list[UTxO],
        collateral: UTxO,
        staking_reward: int = 0,
        min_out: int | None = None,
    ):
        """Build (but do not sign/submit) a Dano swap transaction.

        Returns the unsigned ``Transaction`` object. The caller is
        responsible for signing and submitting.
        """
        from pycardano import Withdrawals  # local import — optional dep path

        pool_change_x, pool_change_y = self.compute_pool_change(
            delta_amount, staking_reward
        )

        # Slippage check
        if delta_amount > 0 and min_out is not None and -pool_change_y < min_out:
            raise InvalidPoolError(
                f"Slippage: out {-pool_change_y} < min_out {min_out}",
            )
        if delta_amount < 0 and min_out is not None and -pool_change_x < min_out:
            raise InvalidPoolError(
                f"Slippage: out {-pool_change_x} < min_out {min_out}",
            )

        new_datum = self.compute_new_datum(delta_amount, swap_fee, cur_epoch)

        # New pool assets = old pool utxo assets + signed changes;
        # when X is ADA the swap_fee is paid into lovelace as well.
        new_pool_assets = Assets(root=dict(self.assets.root))
        if self.dex_nft is not None:
            new_pool_assets.root[self.dex_nft.unit()] = 1  # preserve validity NFT
        lovelace_delta = pool_change_x if self.unit_a == "lovelace" else 0
        if self.unit_a == "lovelace":
            new_pool_assets.root["lovelace"] = (
                new_pool_assets.root.get("lovelace", 0) + pool_change_x + swap_fee
            )
            new_pool_assets.root[self.unit_b] = (
                new_pool_assets.root.get(self.unit_b, 0) + pool_change_y
            )
        else:
            new_pool_assets.root[self.unit_a] = (
                new_pool_assets.root.get(self.unit_a, 0) + pool_change_x
            )
            new_pool_assets.root[self.unit_b] = (
                new_pool_assets.root.get(self.unit_b, 0) + pool_change_y
            )
            # Non-ADA pools still accrue swap_fee as lovelace in the pool UTxO.
            new_pool_assets.root["lovelace"] = (
                new_pool_assets.root.get("lovelace", 0) + swap_fee
            )
        del lovelace_delta

        new_pool_output = TransactionOutput(
            address=Address.from_primitive(DANO_POOL_ADDRESS_MAINNET),
            amount=asset_to_value(new_pool_assets),
            datum=new_datum,
        )

        # The tx is built, then we stamp redeemer bytes after input sorting
        # (pycardano sorts canonically). We pre-compute the indices by
        # sorting ourselves.
        spend_inputs = sorted(
            [pool_utxo, *user_inputs],
            key=lambda u: (bytes(u.input.transaction_id), u.input.index),
        )
        pool_in_idx = spend_inputs.index(pool_utxo)
        pool_out_idx = 0  # the new pool output is always output 0

        ref_inputs = sorted(
            [protocol_config_utxo, pool_script_ref_utxo],
            key=lambda u: (bytes(u.input.transaction_id), u.input.index),
        )
        protocol_config_idx = ref_inputs.index(protocol_config_utxo)

        spend_redeemer_bytes = build_swap_redeemer_bytes(
            delta_amount=delta_amount,
            pool_in_idx=pool_in_idx,
            pool_out_idx=pool_out_idx,
            first_byte=pool_in_idx,
        )
        withdraw_redeemer_bytes = build_swap_redeemer_bytes(
            delta_amount=delta_amount,
            pool_in_idx=pool_in_idx,
            pool_out_idx=pool_out_idx,
            first_byte=protocol_config_idx,
        )

        tb = TransactionBuilder(context)
        for u in user_inputs:
            tb.add_input(u)
        tb.add_script_input(
            pool_utxo,
            script=pool_script_ref_utxo,
            redeemer=Redeemer(RawCBOR(spend_redeemer_bytes)),
        )
        tb.reference_inputs.add(protocol_config_utxo)
        tb.collaterals.append(collateral)
        tb.add_output(new_pool_output)

        # Withdraw-zero trick on the pool reward address.
        reward_addr = Address.from_primitive(DANO_POOL_REWARD_ADDRESS_MAINNET)
        tb.withdrawals = Withdrawals({bytes(reward_addr): 0})
        tb.add_withdrawal_script(
            pool_script_ref_utxo,
            Redeemer(RawCBOR(withdraw_redeemer_bytes)),
        )

        return tb.build(change_address=address_source)

    # --- post-init ---------------------------------------------------------

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        values = super().post_init(values)
        # Surface lp_fee_rate on the model so `volume_fee` works out of the box.
        datum = cls.pool_datum_class().from_cbor(values["datum_cbor"])
        values["fee"] = datum.lp_fee_rate
        return values

    @property
    def tvl(self) -> Decimal:
        if self.unit_a != "lovelace":
            raise NotImplementedError("TVL only implemented for ADA pools.")
        return 2 * (Decimal(self.reserve_a) / Decimal(10**6)).quantize(
            Decimal(1) / Decimal(10**6),
        )
