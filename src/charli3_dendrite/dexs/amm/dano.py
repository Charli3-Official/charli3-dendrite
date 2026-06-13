"""Dano Concentrated Liquidity (CLMM) DEX module.

Port of the Dano CLMM SDK (TypeScript) to Dendrite. Dano executes swaps
directly against the pool with a custom packed-bytes redeemer rather than
via a batcher/order-datum flow, so the `swap_utxo` implementation here
follows the direct-spend pattern established by `splash.py`:

  * `swap_utxo` mutates the caller's `TransactionBuilder` to add the pool
    spend input, the protocol-config reference input, and the withdraw-
    zero-trick withdrawal. It returns the new pool output + its datum;
    the caller is responsible for user-side funding inputs, receive
    outputs, collateral, fees, and submission.
  * The caller must add the protocol-config UTxO to
    `tx_builder.reference_inputs` before calling `swap_utxo`. The method
    reads `platform_fee_rate` and `swap_fee` from its datum.
"""

import time
from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal
from math import isqrt
from typing import Any
from typing import ClassVar

from pycardano import Address
from pycardano import IndefiniteList
from pycardano import PlutusData
from pycardano import PlutusV3Script
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Withdrawals
from pycardano import plutus_script_hash
from pycardano.serialization import CBORSerializable

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import OrderType
from charli3_dendrite.dataclasses.datums import PoolDatum
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.amm.amm_types import AbstractConstantLiquidityPoolState
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

# Pool addresses on mainnet. ADA pools have a staking part (addr1x…);
# non-ADA pools do not (addr1w…). Both share the same payment script hash.
DANO_POOL_ADDRESS_ADA_MAINNET = (
    "addr1x8vtd879xcmme7kmc3rfpqlhq67zj06dn53fvervtjsk0w"
    "7dwgsd23ac468cjj8rcnyuc3s72rtupu6j9dw0xpw83exsufvrg4"
)
DANO_POOL_ADDRESS_NONADA_MAINNET = (
    "addr1w8vtd879xcmme7kmc3rfpqlhq67zj06dn53fvervtjsk0wc7a283u"
)
# Reward address used for the withdraw-zero trick. Its script hash equals
# the pool payment-script hash, so the same reference script serves both
# the spend and reward redeemer.
DANO_POOL_REWARD_ADDRESS_MAINNET = (
    "stake178vtd879xcmme7kmc3rfpqlhq67zj06dn53fvervtjsk0wczh2pdw"
)
# Reference UTxO out-refs — mainnet (see CLMM/clmm-sdk-init-sdk/src/constants.ts)
POOL_SCRIPT_OUT_REF_MAINNET = (
    "64d111b957e7d7848ffdde5149aa77fa4090a7fa1ad0ac108067900614848501",
    0,
)
PROTOCOL_CONFIG_OUT_REF_MAINNET = (
    "2cafd7c92f7093e5229af274be83dea660b0590b4174bbed79ba662b44fbd1ee",
    0,
)

# Cardano epoch derivation (spec § Common Temporary Variables > curEpoch)
EPOCH_BOUNDARY_MS_MAINNET = 1_647_899_091_000
EPOCH_LENGTH_MS_MAINNET = 432_000_000
EPOCH_BOUNDARY_AS_EPOCH_MAINNET = 328
# Slot at the same epoch-328 boundary (post-Shelley: 1 slot = 1 second).
# Lets us derive the current slot from wall clock, consistent with the epoch
# above, so the swap tx's validity range encodes the same epoch we write into
# the pool datum (the withdraw validator cross-checks them).
SLOT_AT_EPOCH_BOUNDARY_MAINNET = 56_332_800


def _parse_protocol_config_datum(datum_cbor: str) -> tuple[int, int]:
    """Return ``(platform_fee_rate, swap_fee)`` from a protocol-config datum."""
    raw = RawPlutusData.from_cbor(datum_cbor)
    fields = raw.data.value
    return int(fields[0]), int(fields[1])


def _current_epoch_mainnet(now_ms: int | None = None) -> int:
    t_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return (
        t_ms - EPOCH_BOUNDARY_MS_MAINNET
    ) // EPOCH_LENGTH_MS_MAINNET + EPOCH_BOUNDARY_AS_EPOCH_MAINNET


def _current_slot_mainnet(now_ms: int | None = None) -> int:
    """Current mainnet slot from wall clock (post-Shelley: 1 slot/second)."""
    t_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return SLOT_AT_EPOCH_BOUNDARY_MAINNET + (t_ms - EPOCH_BOUNDARY_MS_MAINNET) // 1000


def _find_protocol_config(tx_builder: TransactionBuilder) -> UTxO | None:
    """Scan ``tx_builder.reference_inputs`` for the protocol-config UTxO."""
    wanted_tx, wanted_idx = PROTOCOL_CONFIG_OUT_REF_MAINNET
    wanted_tx_bytes = bytes.fromhex(wanted_tx)
    for utxo in tx_builder.reference_inputs:
        if (
            bytes(utxo.input.transaction_id) == wanted_tx_bytes
            and utxo.input.index == wanted_idx
        ):
            return utxo
    return None


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
class DanoSwapRedeemer(CBORSerializable):
    """Single-pool Dano swap redeemer (Spend or Withdraw).

    The on-chain redeemer Plutus Data is the raw swap payload serialized as a
    CBOR *byte string* (``0x58<len><payload>``); :meth:`to_primitive` returns
    the payload so it serializes that way. The pool input index and the first
    byte (``pool_in_idx`` for Spend, the protocol-config reference index for
    Withdraw) depend on the FINAL transaction ordering, so they are resolved
    post-build by :meth:`set_idx`, which the tx builder calls after sorting and
    indexing inputs (mirrors ``SSPoolRedeemer.set_idx`` and the clmm-sdk's
    ``RedeemerArg`` callback). ``pool_out_idx`` is the pool's position in the
    swap's batch vector — ``0`` for a single-pool swap — NOT a tx output index.
    """

    pool_input: TransactionInput
    delta_amount: int
    is_withdraw: bool
    protocol_config_input: TransactionInput | None = None
    pool_out_idx: int = 0
    pool_in_idx: int = 0
    first_byte: int = 0

    @staticmethod
    def _sorted_index(utxos: list, target: TransactionInput) -> int:
        # Canonical order: matches the input ordering the builder applies
        # before assigning redeemer indices (str(tx_id), index).
        ordered = sorted(
            utxos,
            key=lambda u: (str(u.input.transaction_id), u.input.index),
        )
        for i, utxo in enumerate(ordered):
            if utxo.input == target:
                return i
        raise InvalidPoolError("redeemer UTxO not found in transaction during set_idx")

    def set_idx(self, tx_builder: TransactionBuilder) -> None:
        """Resolve pool/config indices from the final (sorted) transaction."""
        self.pool_in_idx = self._sorted_index(list(tx_builder.inputs), self.pool_input)
        if self.is_withdraw:
            if self.protocol_config_input is None:
                raise InvalidPoolError(
                    "withdraw redeemer missing protocol-config input",
                )
            self.first_byte = self._sorted_index(
                list(tx_builder.reference_inputs),
                self.protocol_config_input,
            )
        else:
            self.first_byte = self.pool_in_idx

    def to_primitive(self) -> bytes:
        """Serialize the redeemer as the packed swap payload bytestring."""
        return build_swap_redeemer_bytes(
            delta_amount=self.delta_amount,
            pool_in_idx=self.pool_in_idx,
            pool_out_idx=self.pool_out_idx,
            first_byte=self.first_byte,
        )

    @classmethod
    def from_primitive(
        cls,
        value: object,  # noqa: ARG003
        type_args: object = None,  # noqa: ARG003
    ) -> "DanoSwapRedeemer":
        """Raise: the redeemer is build-only and cannot be deserialized."""
        msg = "DanoSwapRedeemer is build-only and cannot be deserialized"
        raise NotImplementedError(msg)


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
        """Return tokenX as a Dendrite asset unit."""
        return _bytes_pair_to_unit(self.token_x)

    @property
    def unit_y(self) -> str:
        """Return tokenY as a Dendrite asset unit."""
        return _bytes_pair_to_unit(self.token_y)

    def pool_pair(self) -> Assets | None:
        """Return the pool's (tokenX, tokenY) asset pair."""
        return Assets(root={self.unit_x: 0, self.unit_y: 0})


@dataclass
class DanoOrderDatum(OrderDatum):
    """Stub OrderDatum for Dano CLMM.

    Dano uses direct-spend swaps, not a batcher/order-datum flow.
    This class exists only to satisfy the Dendrite test suite's
    `test_order_type` check (issubclass(order_datum_class(), OrderDatum)).
    It intentionally has no `create_datum` so `test_address_from_datum`
    auto-skips via the hasattr guard.
    """

    CONSTR_ID = 255  # unused on-chain

    def address_source(self) -> Address:
        """Raise: Dano uses direct-spend swaps, not order datums."""
        raise NotImplementedError("Dano does not use order datums")

    def requested_amount(self) -> Assets:
        """Raise: Dano uses direct-spend swaps, not order datums."""
        raise NotImplementedError("Dano does not use order datums")

    def order_type(self) -> OrderType:
        """Raise: Dano uses direct-spend swaps, not order datums."""
        raise NotImplementedError("Dano does not use order datums")


class DanoCLMMState(AbstractConstantLiquidityPoolState):
    """State of a single Dano concentrated-liquidity pool.

    The single-band CLMM math (``virtual_reserves`` + ``get_amount_out``/
    ``get_amount_in`` + the capacity cap) is inherited from
    :class:`AbstractConstantLiquidityPoolState`; this class supplies only the
    Dano-specific datum parsing, active-reserve carve-outs, and direct-spend
    tx-building. The LP fee is surfaced as ``fee`` in :meth:`post_init` so the
    base reads it via ``volume_fee``/``_lp_fee_rate``.
    """

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
    _reference_utxo: ClassVar[UTxO | None] = None

    @classmethod
    def dex(cls) -> str:
        """Return the name of the DEX."""
        return "Dano"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return the order selector addresses."""
        # No off-chain order address: swaps are direct.
        return [cls._stake_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return the pool selector for this DEX."""
        return PoolSelector(
            addresses=[
                DANO_POOL_ADDRESS_ADA_MAINNET,
                DANO_POOL_ADDRESS_NONADA_MAINNET,
            ],
            assets=cls.dex_policy(),
        )

    @classmethod
    def reference_utxo(cls) -> UTxO | None:
        """Pool script reference UTxO (shared by spend + reward redeemers).

        The Dano pool validator is a **PlutusV3** script (its v3 hash equals
        the pool address payment credential). The shared
        ``ScriptReference.to_utxo()`` hardcodes ``PlutusV2Script``, which would
        yield the wrong script hash and make every swap tx invalid, so re-type
        the reference script as ``PlutusV3Script`` here and assert it matches
        the known pool script hash before caching it.
        """
        if cls._reference_utxo is None:
            script_ref = get_backend().get_script_from_address(cls._stake_address)
            if script_ref is None or script_ref.script is None:
                return None
            utxo = script_ref.to_utxo()
            if utxo is None:
                return None
            v3_script = PlutusV3Script(bytes.fromhex(script_ref.script))
            if str(plutus_script_hash(v3_script)) != DANO_POOL_SCRIPT_HASH_MAINNET:
                raise InvalidPoolError(
                    "Dano reference script hash does not match the pool "
                    f"validator ({DANO_POOL_SCRIPT_HASH_MAINNET})",
                )
            utxo.output.script = v3_script
            cls._reference_utxo = utxo
        return cls._reference_utxo

    def _staking_reference(self, pool_address: str) -> tuple[UTxO, Address]:
        """Per-pool staking script reference UTxO + reward address.

        ADA pools delegate their lovelace via a per-pool staking script whose
        hash is the delegation part of the pool address (addr1x…). When the pool
        is overdue the swap must withdraw that stake account's rewards, which
        needs the staking script as a reference input. Like the pool script it
        is deployed on-chain keyed by its hash, so resolve it via
        ``get_script_from_address`` and re-type it as ``PlutusV3Script``.
        """
        addr = Address.decode(pool_address)
        stake_cred = addr.staking_part
        if not isinstance(stake_cred, ScriptHash):
            raise InvalidPoolError(
                "Overdue Dano ADA pool address has no script staking credential",
            )
        reward_addr = Address(staking_part=stake_cred, network=addr.network)
        sref = get_backend().get_script_from_address(
            Address(payment_part=stake_cred, network=addr.network),
        )
        if sref is None or sref.script is None:
            raise InvalidPoolError(
                "Dano staking script reference UTxO unavailable from backend",
            )
        utxo = sref.to_utxo()
        if utxo is None:
            raise InvalidPoolError(
                "Dano staking script reference UTxO unavailable from backend",
            )
        v3_script = PlutusV3Script(bytes.fromhex(sref.script))
        if bytes(plutus_script_hash(v3_script)) != bytes(stake_cred):
            raise InvalidPoolError(
                "Dano staking reference script hash does not match the pool "
                "staking credential",
            )
        utxo.output.script = v3_script
        return utxo, reward_addr

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """Return the validity-NFT policy id (the pool script hash)."""
        # Dano mints the validity NFT from the pool validator itself,
        # so the policy id == pool script hash. The 32-byte asset name is
        # a per-pool unique identifier.
        return [DANO_POOL_SCRIPT_HASH_MAINNET]

    @classmethod
    def pool_policy(cls) -> list[str] | None:
        """Return None: Dano uses a single NFT already covered by dex_policy."""
        # The validity NFT is already extracted by dex_policy(); Dano
        # uses a single NFT for both roles, so don't double-count.
        return None

    @classmethod
    def default_script_class(cls) -> type[PlutusV3Script]:
        """Return the default script class (PlutusV3) for Dano pools."""
        # Dano pool validators are PlutusV3 (verified on-chain: the script's
        # v3 hash equals the pool address payment credential).
        return PlutusV3Script

    @property
    def swap_forward(self) -> bool:
        """Return whether this DEX supports swap forwarding."""
        return False

    @property
    def stake_address(self) -> Address:
        """Return the pool's stake address."""
        return self._stake_address

    @classmethod
    def pool_datum_class(cls) -> type[DanoPoolDatum]:
        """Return the pool datum class for this DEX."""
        return DanoPoolDatum

    @classmethod
    def order_datum_class(cls) -> type[DanoOrderDatum]:
        """Return the order datum class for this DEX."""
        return DanoOrderDatum

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool."""
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
    def unit_a(self) -> str:
        """Return tokenX's asset unit (the canonical a-side)."""
        return self._datum.unit_x

    @property
    def unit_b(self) -> str:
        """Return tokenY's asset unit (the canonical b-side)."""
        return self._datum.unit_y

    @property
    def raw_x(self) -> int:
        """Return the raw tokenX balance held in the pool UTxO."""
        return self.assets[self._datum.unit_x]

    @property
    def raw_y(self) -> int:
        """Return the raw tokenY balance held in the pool UTxO."""
        return self.assets[self._datum.unit_y]

    @property
    def reserve_a(self) -> int:
        """Active reserve of tokenX, net of platform fees and ADA carve-out."""
        d = self._datum
        excluded_ada = ADA_MIN_UTXO + d.total_swap_fee if d.unit_x == "lovelace" else 0
        return self.raw_x - d.platform_fee_x - excluded_ada

    @property
    def reserve_b(self) -> int:
        """Active reserve of tokenY, net of platform fees."""
        return self.raw_y - self._datum.platform_fee_y

    # --- math (single-band CLMM curve inherited from the base) -------------
    # ``virtual_reserves()`` + ``get_amount_out()``/``get_amount_in()`` + the
    # capacity cap live on ``AbstractConstantLiquidityPoolState``; Dano supplies
    # only the band bounds below. ``unit_a``/``unit_b`` == tokenX/tokenY and
    # ``reserve_a``/``reserve_b`` are the active (carve-out-netted) reserves, so
    # the base reproduces the original utils.ts:calculateConcentratedPoolSwap
    # port exactly. The tx-build path (``compute_pool_change``) keeps its own
    # virtual-reserve computation because it must net an in-tx staking reward.

    def _sqrt_price_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Band sqrt-price bounds: ``((lower_n, lower_d), (upper_n, upper_d))``."""
        d = self._datum
        return (
            (d.sqrt_lower_price.numerator, d.sqrt_lower_price.denominator),
            (d.sqrt_upper_price.numerator, d.sqrt_upper_price.denominator),
        )

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
            pool_in_lp_x = x_raw - d.platform_fee_x - d.total_swap_fee - ADA_MIN_UTXO
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
            diff * diff + 4 * pool_in_lp_x * pool_in_lp_y * pa_d * pa_d * pb_n * pb_n,
        )
        liq_num = pool_in_lp_y * den_ab + pool_in_lp_x * num_ab + big
        liq_den = 2 * (pb_n * pa_d - pb_d * pa_n)

        x_v = -(-(liq_num * pb_d) // (liq_den * pb_n)) + pool_in_lp_x
        y_v = -(-(liq_num * pa_n) // (liq_den * pa_d)) + pool_in_lp_y

        off_fee = FEE_BASIS - d.lp_fee_rate  # basis minus fee rate

        if delta_amount > 0:
            # poolChangeY is the new tokenY level ceil(Xv*Yv / (Xv +
            # poolChangeX*offFee)) minus Yv, clamped below at -poolinLPY,
            # rewritten here in exact integer arithmetic.
            numerator = x_v * y_v * FEE_BASIS
            denom = x_v * FEE_BASIS + delta_amount * off_fee
            # y_new_ceil is ceil(numerator/denom).
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

    def _delta_from_in_assets(self, in_assets: Assets) -> int:
        """Signed redeemer delta_amount from user's intended input."""
        d = self._datum
        if len(in_assets) != 1:
            raise ValueError("in_assets must contain exactly one token")
        unit = in_assets.unit()
        qty = in_assets.quantity()
        if unit == d.unit_x:
            return qty
        if unit == d.unit_y:
            return -qty
        raise ValueError(f"Asset {unit} does not belong to pool {d.unit_x}/{d.unit_y}")

    def _new_pool_assets(
        self,
        pool_change_x: int,
        pool_change_y: int,
        swap_fee: int,
        staking_reward: int = 0,
    ) -> Assets:
        d = self._datum
        new = Assets(root=dict(self.assets.root))
        if self.dex_nft is not None:
            new.root[self.dex_nft.unit()] = 1
        new.root[d.unit_x] = new.root.get(d.unit_x, 0) + pool_change_x
        new.root[d.unit_y] = new.root.get(d.unit_y, 0) + pool_change_y
        # swap_fee (always lovelace) plus any staking reward withdrawn into the
        # pool stay in the pool's lovelace. The withdraw validator REQUIRES the
        # reward to land in the pool output, not the user's change (verified on
        # Ogmios: reward-to-change fails 3012, reward-in-pool passes).
        new.root["lovelace"] = new.root.get("lovelace", 0) + swap_fee + staking_reward
        return new

    def swap_utxo(  # noqa: C901, PLR0912, PLR0915
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder | None = None,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        staking_reward: int | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Wire a direct-spend Dano swap into ``tx_builder``.

        Follows the Splash-style contract: this mutates ``tx_builder`` to
        add the pool script input and the withdraw-zero-trick withdrawal,
        then returns the new pool output + datum so the caller can add it
        with ``tx_builder.add_output(...)``.

        Caller contract:
          * Add user funding inputs to ``tx_builder`` FIRST.
          * Add the protocol-config UTxO to
            ``tx_builder.reference_inputs`` FIRST — it's used as a
            reference input AND its datum is parsed here to derive
            ``platform_fee_rate`` / ``swap_fee``.
          * Call this method; add the returned output to the builder.
          * Add the user's receive output + collateral, then build/sign.

        ``in_assets`` must carry exactly one asset belonging to this pool
        (either ``datum.unit_x`` or ``datum.unit_y``) with the user's
        intended input quantity.
        """
        if tx_builder is None:
            raise NotImplementedError(
                "tx_builder is required for Dano swap construction",
            )
        if self.tx_hash is None or self.tx_index is None:
            raise ValueError("pool tx_hash/tx_index required (fetched from backend)")
        if self.dex_nft is None:
            raise InvalidPoolError("pool UTxO has no validity NFT")

        # Protocol config must be a reference input on tx_builder.
        pc_utxo = _find_protocol_config(tx_builder)
        if pc_utxo is None:
            raise ValueError(
                "Protocol-config UTxO must be added to tx_builder.reference_inputs "
                "before calling swap_utxo (out-ref "
                f"{PROTOCOL_CONFIG_OUT_REF_MAINNET[0]}#{PROTOCOL_CONFIG_OUT_REF_MAINNET[1]})",
            )
        pc_datum = pc_utxo.output.datum if pc_utxo.output.datum is not None else None
        if pc_datum is None:
            raise ValueError("Protocol-config UTxO is missing its inline datum")
        self.platform_fee_rate, swap_fee = _parse_protocol_config_datum(
            pc_datum.to_cbor_hex()
            if hasattr(pc_datum, "to_cbor_hex")
            else pc_datum.cbor.hex(),
        )

        # Derive epoch AND slot from a single wall-clock reading so the pool
        # datum's last_withdraw_epoch and the tx validity range (set below)
        # encode the same epoch — the withdraw validator cross-checks them.
        now_ms = int(time.time() * 1000)
        cur_epoch = _current_epoch_mainnet(now_ms)

        delta_amount = self._delta_from_in_assets(in_assets)

        # Rebuild the pool input UTxO. Address is fetched from the pool tx so we
        # pick up the correct form (addr1x… or addr1w…); the addr1x delegation
        # part also carries the per-pool staking credential used below.
        pool_in_assets = Assets(root=dict(self.assets.root))
        if self.dex_nft is not None:
            pool_in_assets.root[self.dex_nft.unit()] = 1

        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.dex_nft.unit()],
            addresses=self.pool_selector().addresses,
        )
        if not order_info:
            raise InvalidPoolError("Could not re-fetch pool UTxO address via backend")
        pool_address = order_info[0].address

        # Overdue ADA pool: when curEpoch > last_withdraw_epoch the validator
        # requires the pool's accrued staking rewards to be withdrawn from the
        # per-pool stake credential in the SAME tx (a second withdrawal) and the
        # reward folded into the pool's effective liquidity. The reward is often
        # zero (registered but not yet matured), but the withdrawal must still
        # be present for the validator to accept the last_withdraw_epoch bump.
        # Token/token pools have no ADA stake and are unaffected.
        is_overdue = (
            self._datum.unit_x == "lovelace"
            and cur_epoch > self._datum.last_withdraw_epoch
        )
        staking_ref_utxo: UTxO | None = None
        staking_reward_addr: Address | None = None
        if is_overdue:
            staking_ref_utxo, staking_reward_addr = self._staking_reference(
                pool_address,
            )
            if staking_reward is None:
                staking_reward = get_backend().get_stake_rewards(staking_reward_addr)
        reward = staking_reward or 0

        pool_change_x, pool_change_y = self.compute_pool_change(delta_amount, reward)

        # Capacity guard. compute_pool_change CLAMPS the output to the band
        # reserve when a swap would drain the whole concentrated-liquidity band,
        # but the validator REJECTS that clamped swap on-chain (verified via
        # Ogmios: every clamped build fails, every strictly-in-range build —
        # up to 99.99% of capacity — passes; the full reserve is unreachable
        # with finite input, which is also why get_amount_in raises there).
        # Refuse rather than emit a tx that cannot validate; callers size the
        # input with get_amount_in to stay strictly in range.
        lp_x, lp_y = self.active_liquidity(reward)
        if (delta_amount > 0 and pool_change_y <= -lp_y) or (
            delta_amount < 0 and pool_change_x <= -lp_x
        ):
            raise InvalidPoolError(
                "Swap exceeds the pool's concentrated-liquidity band capacity; "
                "the validator rejects a capacity-clamped swap. Size the input "
                "with get_amount_in to stay strictly in range.",
            )

        # Slippage: out_assets.quantity() is caller's min_out.
        computed_out = -pool_change_y if delta_amount > 0 else -pool_change_x
        if out_assets is not None and out_assets.quantity() > computed_out:
            raise InvalidPoolError(
                f"Slippage: computed_out={computed_out} < requested "
                f"min_out={out_assets.quantity()}",
            )

        new_datum = self.compute_new_datum(delta_amount, swap_fee, cur_epoch)
        new_pool_assets = self._new_pool_assets(
            pool_change_x,
            pool_change_y,
            swap_fee,
            reward,
        )

        input_utxo = UTxO(
            input=TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(pool_address),
                amount=asset_to_value(pool_in_assets),
                datum=self._datum,
            ),
        )

        new_pool_output = TransactionOutput(
            address=Address.decode(pool_address),
            amount=asset_to_value(new_pool_assets),
            datum=new_datum,
        )

        script_ref = self.reference_utxo()
        if script_ref is None:
            raise InvalidPoolError(
                "Pool script reference UTxO unavailable from backend",
            )

        # The spend + withdraw redeemers encode the pool input index and the
        # first byte (pool input index for Spend, protocol-config reference
        # index for Withdraw), which depend on the FINAL sorted inputs /
        # reference inputs. The builder may still trim or reorder inputs after
        # this call, so the indices are resolved POST-BUILD by
        # ``DanoSwapRedeemer.set_idx`` — the tx builder runs it after assigning
        # redeemer indices (mirrors Splash's pool redeemer + the clmm-sdk's
        # ``RedeemerArg`` callback). ``pool_out_idx`` is the single-pool batch
        # position (0), not a tx output index.
        spend_redeemer = DanoSwapRedeemer(
            pool_input=input_utxo.input,
            delta_amount=delta_amount,
            is_withdraw=False,
        )
        withdraw_redeemer = DanoSwapRedeemer(
            pool_input=input_utxo.input,
            delta_amount=delta_amount,
            is_withdraw=True,
            protocol_config_input=pc_utxo.input,
        )

        tx_builder.add_script_input(
            utxo=input_utxo,
            script=script_ref,
            redeemer=Redeemer(spend_redeemer),
        )

        reward_addr = Address.from_primitive(DANO_POOL_REWARD_ADDRESS_MAINNET)
        withdrawals: dict[bytes, int] = {bytes(reward_addr): 0}
        if is_overdue and staking_reward_addr is not None:
            withdrawals[bytes(staking_reward_addr)] = reward
        tx_builder.withdrawals = Withdrawals(withdrawals)
        tx_builder.add_withdrawal_script(
            script_ref,
            Redeemer(withdraw_redeemer),
        )
        if is_overdue and staking_ref_utxo is not None:
            # Per-pool staking-reward withdrawal. Its redeemer is pool-input
            # indexed (like Spend, NOT the protocol-config-indexed withdraw-zero).
            # add_withdrawal_script also registers the staking script reference
            # input automatically.
            staking_redeemer = DanoSwapRedeemer(
                pool_input=input_utxo.input,
                delta_amount=delta_amount,
                is_withdraw=False,
            )
            tx_builder.add_withdrawal_script(
                staking_ref_utxo,
                Redeemer(staking_redeemer),
            )

        # The withdraw validator derives curEpoch from the tx validity range and
        # requires it to match new_datum.last_withdraw_epoch. Without a validity
        # range the script terminates. Mirror the clmm-sdk's
        # setValidity(now-2min, now+4min), derived from the same now_ms used for
        # cur_epoch so both encode the same epoch. (Edge case: a swap built in
        # the first 2 min / last 4 min of an epoch can straddle the boundary —
        # the clmm-sdk has the same exposure.)
        cur_slot = _current_slot_mainnet(now_ms)
        if tx_builder.validity_start is None:
            tx_builder.validity_start = cur_slot - 120
        if tx_builder.ttl is None:
            tx_builder.ttl = cur_slot + 240

        return new_pool_output, new_datum

    # --- post-init ---------------------------------------------------------

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Post-initialization processing: surface lp_fee_rate on the model."""
        values = super().post_init(values)
        # Surface lp_fee_rate on the model so `volume_fee` works out of the box.
        datum = cls.pool_datum_class().from_cbor(values["datum_cbor"])
        values["fee"] = datum.lp_fee_rate
        return values

    @property
    def tvl(self) -> Decimal:
        """Return the pool's total value locked, in ADA."""
        if self.unit_a != "lovelace":
            raise NotImplementedError("TVL only implemented for ADA pools.")
        return 2 * (Decimal(self.reserve_a) / Decimal(10**6)).quantize(
            Decimal(1) / Decimal(10**6),
        )
