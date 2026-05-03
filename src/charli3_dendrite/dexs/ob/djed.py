"""Djed/Shen Stablecoin Order Book Module.

This module provides order book functionality for Djed (collateralized stablecoin)
and Shen (liquidity token) operations, following the exact patterns established
by the GeniusYield implementation.
"""

import math
import time
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import min_lovelace

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.utility import asset_to_value

# Djed/Shen mainnet asset IDs (policy_id + asset_name hex)
DJED_TOKEN = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
    "446a65644d6963726f555344"
)
SHEN_TOKEN = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
    "5368656e4d6963726f555344"
)
POOL_NFT = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
    "446a6564537461626c65436f696e4e4654"
)
ORDER_NFT_POLICY = "04ea363a127872366ef2d3186325a25a5cee8826ff8a79dc7c8fa671"
ORDER_NFT_NAME_HEX = "446a65644f726465725469636b6574"

# Shelley mainnet genesis parameters - Necessary for order datum creation
SHELLEY_START_POSIX = 1596491091  # Unix timestamp when Shelley started
SHELLEY_START_SLOT = 4924800  # Slot number when Shelley started
MIN_RESERVE_RATIO_PERCENT = 400
MAX_RESERVE_RATIO_PERCENT = 800
MIN_ORDER_AMOUNT = 50_000_000
FEE_NUMERATOR = 15
FEE_DENOMINATOR = 1000


def _slot_to_posix_ms(slot: int) -> int:
    """Convert a Cardano slot number to POSIX milliseconds (mainnet only)."""
    return (slot - SHELLEY_START_SLOT + SHELLEY_START_POSIX) * 1000


@dataclass
class DjedRational(PlutusData):
    """Plutus-compatible rational number for on-chain data.

    IMPORTANT: Field order matches open-djed TypeScript - denominator first.
    """

    CONSTR_ID = 0
    denominator: int
    numerator: int


@dataclass
class DjedProcessOrderRedeemer(PlutusData):
    """Redeemer for processing orders (spending order UTxO)."""

    CONSTR_ID = 0


@dataclass
class DjedCancelOrderRedeemer(PlutusData):
    """Redeemer for canceling orders / burning order NFT."""

    CONSTR_ID = 1


@dataclass
class DjedOrderMintRedeemer(PlutusData):
    """Redeemer for minting order NFT."""

    CONSTR_ID = 0


@dataclass
class DjedProcessPoolRedeemer(PlutusData):
    """Redeemer for processing pool UTxO."""

    CONSTR_ID = 1


@dataclass
class DjedTxHash(PlutusData):
    """Transaction hash wrapper."""

    CONSTR_ID = 0
    tx_hash: bytes


@dataclass
class DjedOutputReference(PlutusData):
    """Output reference structure for pool datum."""

    CONSTR_ID = 0
    tx_hash: DjedTxHash
    output_index: int


@dataclass
class DjedLastOrderEntry(PlutusData):
    """Last order entry with order reference and timestamp."""

    CONSTR_ID = 0
    order: DjedOutputReference
    time: int


@dataclass
class DjedLastOrder(PlutusData):
    """Wrapper for last order tuple."""

    CONSTR_ID = 0
    entry: DjedLastOrderEntry


@dataclass
class DjedPoolDatumNone(PlutusData):
    """Null/None value for optional fields in pool datum."""

    CONSTR_ID = 1


@dataclass
class DjedPoolDatum(PlutusData):
    """Pool datum containing reserve state and protocol configuration.

    Fields:
    - adaInReserve: How much ADA is in the pool
    - djedInCirculation: How much DJED is in circulation
    - shenInCirculation: How much SHEN is in circulation
    - lastOrder: Last action (mint/burn djed/shen) reference
    - minADA: Minimum ADA for UTxOs
    - reserved1: Reserved field (unknown purpose)
    - reserved2: Nullable reserved field
    - mintingPolicyId: Minting policy of DJED, SHEN and DjedStableCoinNFT
    - mintingPolicyUniqRef: Unique reference for one-shot minting policy
    - reserved3: Reserved output reference
    """

    CONSTR_ID = 0
    ada_in_reserve: int
    djed_in_circulation: int
    shen_in_circulation: int
    last_order: DjedLastOrder
    min_ada: int
    reserved1: int
    reserved2: Union[DjedPoolDatumNone, PlutusData]  # Nullable
    minting_policy_id: bytes
    minting_policy_uniq_ref: DjedOutputReference
    reserved3: DjedOutputReference


@dataclass
class DjedExtendedFinite(PlutusData):
    """Finite timestamp in Extended type (Constructor 1 = Finite)."""

    CONSTR_ID = 1
    time: int


@dataclass
class DjedExtendedPosInf(PlutusData):
    """Positive infinity in Extended type (Constructor 2 = PosInf)."""

    CONSTR_ID = 2


@dataclass
class DjedBoundClosed(PlutusData):
    """Closed bound indicator (Constructor 1 = True)."""

    CONSTR_ID = 1


@dataclass
class DjedBoundOpen(PlutusData):
    """Open bound indicator (Constructor 0 = False)."""

    CONSTR_ID = 0


@dataclass
class DjedLowerBound(PlutusData):
    """Lower bound of validity interval."""

    CONSTR_ID = 0
    bound: Union[DjedExtendedFinite, DjedExtendedPosInf]
    closed: Union[DjedBoundClosed, DjedBoundOpen]


@dataclass
class DjedUpperBound(PlutusData):
    """Upper bound of validity interval."""

    CONSTR_ID = 0
    bound: Union[DjedExtendedFinite, DjedExtendedPosInf]
    closed: Union[DjedBoundClosed, DjedBoundOpen]


@dataclass
class DjedValidityRange(PlutusData):
    """Validity range for oracle data."""

    CONSTR_ID = 0
    lower_bound: DjedLowerBound
    upper_bound: DjedUpperBound


@dataclass
class DjedOracleFields(PlutusData):
    """Oracle fields containing exchange rate and validity information."""

    CONSTR_ID = 0
    ada_usd_exchange_rate: DjedRational  # USD/ADA rate (uses denominator, numerator)
    validity_range: DjedValidityRange
    expressed_in: bytes  # Currency denomination (e.g., b"USD")


@dataclass
class DjedOracleDatum(PlutusData):
    """Oracle datum containing price feed data.

    The oracle provides the ADA/USD exchange rate used to:
    - Calculate Djed mint/burn prices
    - Determine Shen pricing based on excess reserves
    - Validate reserve ratio constraints
    """

    CONSTR_ID = 0
    oracle_signature: bytes  # 64-byte key
    oracle_fields: DjedOracleFields
    oracle_token_policy_id: bytes


@dataclass
class DjedMintAction(PlutusData):
    """Djed minting action in order datum."""

    CONSTR_ID = 0
    djed_amount: int
    ada_amount: int


@dataclass
class DjedBurnAction(PlutusData):
    """Djed burning action in order datum."""

    CONSTR_ID = 1
    djed_amount: int


@dataclass
class ShenMintAction(PlutusData):
    """Shen minting action in order datum."""

    CONSTR_ID = 2
    shen_amount: int
    ada_amount: int


@dataclass
class ShenBurnAction(PlutusData):
    """Shen burning action in order datum."""

    CONSTR_ID = 3
    shen_amount: int


@dataclass
class DjedOrderDatum(OrderDatum):
    """Djed/Shen order datum structure (following existing OrderDatum pattern)."""

    CONSTR_ID = 0
    action: Union[DjedMintAction, DjedBurnAction, ShenMintAction, ShenBurnAction]
    owner_address: PlutusFullAddress
    oracle_rate: DjedRational
    creation_time: int
    order_nft: bytes

    def pool_pair(self) -> Assets | None:
        """Return the asset pair for this order (required by OrderDatum interface)."""
        if isinstance(self.action, (DjedMintAction, DjedBurnAction)):
            # Djed <-> ADA pair
            return Assets(lovelace=0) + Assets(**{DJED_TOKEN: 0})
        # Shen operations - Shen <-> ADA pair
        return Assets(lovelace=0) + Assets(**{SHEN_TOKEN: 0})

    def address_source(self) -> str | None:
        """Source address (required by OrderDatum interface)."""
        return self.owner_address.to_address().encode("bech32")

    def requested_amount(self) -> Assets:
        """Return the requested amount for this order."""
        if isinstance(self.action, DjedMintAction):
            return Assets(**{DJED_TOKEN: self.action.djed_amount})
        if isinstance(self.action, DjedBurnAction):
            # For burn, calculate ADA amount based on oracle rate
            # Invert oracle rate (Djed/ADA -> ADA/Djed) and multiply
            ada_amount = (
                self.action.djed_amount
                * self._oracle_rate.denominator
                // self._oracle_rate.numerator
            )
            return Assets(lovelace=ada_amount)
        if isinstance(self.action, ShenMintAction):
            return Assets(**{SHEN_TOKEN: self.action.shen_amount})
        # ShenBurnAction: exact ADA requires pool state which isn't in datum
        return Assets(lovelace=self.action.shen_amount)

    def order_type(self) -> OrderType | None:
        """Order type classification (required by OrderDatum interface)."""
        if isinstance(self.action, (DjedMintAction, ShenMintAction)):
            return OrderType.deposit  # Minting = deposit operation
        return OrderType.swap  # Burning = swap operation


def _calculate_operator_fee(ada_amount: int) -> int:
    """Calculate Djed operator fee based on ADA amount.

    Fee is 0.25% (1/400) of the ADA amount, clamped between min and max.
    """
    return max(5_150_000, min(25_000_000, ada_amount // 400))


def _finalize_order_tx(
    tx_builder: TransactionBuilder,
    user_address: Address,
    pool_datum: "DjedPoolDatum",
    minting_policy_ref: UTxO,
) -> None:
    """Add common order transaction components (pool datum output, signer, mint NFT)."""
    # Add pool datum hash output to user's address (required by minting script)
    pool_datum_hash_output = TransactionOutput(
        address=user_address,
        amount=asset_to_value(Assets(lovelace=0)),
        datum_hash=pool_datum.hash(),
    )
    pool_datum_hash_output.amount.coin = min_lovelace(
        tx_builder.context,
        output=pool_datum_hash_output,
    )
    tx_builder.add_output(pool_datum_hash_output)

    # Add pool datum to witness set (required when output uses datum_hash)
    if tx_builder.datums is None:
        tx_builder.datums = {}
    tx_builder.datums[pool_datum.hash()] = pool_datum

    # Add user as required signer
    if tx_builder.required_signers is None:
        tx_builder.required_signers = []
    tx_builder.required_signers.append(user_address.payment_part)

    # Mint the order NFT (+1)
    tx_builder.add_minting_script(
        script=minting_policy_ref,
        redeemer=Redeemer(DjedOrderMintRedeemer()),
    )
    mint_assets = Assets(**{ORDER_NFT_POLICY + ORDER_NFT_NAME_HEX: 1})
    if tx_builder.mint is None:
        tx_builder.mint = asset_to_value(mint_assets).multi_asset
    else:
        tx_builder.mint += asset_to_value(mint_assets).multi_asset


class DjedShenOrderBookBase(AbstractOrderBookState):
    """Base class for Djed/Shen order books sharing common functionality."""

    fee: int = 150  # 1.5% fee in basis points
    _deposit: Assets = Assets(lovelace=3_000_000)

    # Snapshot state populated once during get_book()
    _oracle_rate: DjedRational
    _pool_datum: DjedPoolDatum
    _oracle_utxo: UTxO
    _pool_utxo: UTxO
    _minting_policy_ref: UTxO

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order contract address (shared)."""
        return [
            "addr1wypp5vhw2csaf62d78vmaa4652z20nr4hfgmkhacqnrvgug2vdyq4",
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection for Djed/Shen orders (shared)."""
        pool_addr = (
            "addr1z8mcpc26j64fmhhd6sv5qj5mk9xqnfxgm6k8zmk7h2rlu4"
            "qm5kjdmrpmng059yellupyvwgay2v0lz6663swmds7hp0qhxg9gt"
        )
        return PoolSelector(
            addresses=[pool_addr],
            assets=[POOL_NFT],
        )

    @classmethod
    def oracle_selector(cls) -> PoolSelector:
        """Oracle selection for Djed/Shen (shared)."""
        oracle_nft = (
            "815aca02042ba9188a2ca4f8ce7b276046e2376b4bce56391342299e"
            "446a65644f7261636c654e4654"
        )
        return PoolSelector(
            addresses=["addr1wxyc99q448xlkv4q2y3truxq7j2msr6hkqqg0wmzz9n9r6q8j7kpa"],
            assets=[oracle_nft],
        )

    @classmethod
    def _get_oracle_utxo_and_datum(cls) -> tuple[UTxO, DjedOracleDatum]:
        """Get oracle UTxO and datum in a single fetch (avoids race conditions)."""
        selector = cls.oracle_selector()
        oracle_utxos = get_backend().get_pool_utxos(
            limit=1,
            historical=False,
            **selector.model_dump(),
        )
        if not oracle_utxos:
            raise RuntimeError("Oracle UTxO not found")
        oracle_info = oracle_utxos[0]
        datum = DjedOracleDatum.from_cbor(oracle_info.datum_cbor)
        utxo = UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(oracle_info.tx_hash)),
                index=oracle_info.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(oracle_info.address),
                amount=asset_to_value(oracle_info.assets),
                datum=datum,
            ),
        )
        return utxo, datum

    @classmethod
    def _get_pool_utxo_and_datum(cls) -> tuple[UTxO, DjedPoolDatum]:
        """Get pool UTxO and datum in a single fetch (avoids race conditions)."""
        selector = cls.pool_selector()
        pool_utxos = get_backend().get_pool_utxos(
            limit=1,
            historical=False,
            **selector.model_dump(),
        )
        if not pool_utxos:
            raise RuntimeError("Pool UTxO not found")
        pool_info = pool_utxos[0]
        datum = DjedPoolDatum.from_cbor(pool_info.datum_cbor)
        utxo = UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(pool_info.tx_hash)),
                index=pool_info.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(pool_info.address),
                amount=asset_to_value(pool_info.assets),
                datum=datum,
            ),
        )
        return utxo, datum

    @classmethod
    def _get_minting_policy_ref_utxo(cls) -> UTxO:
        """Get the order minting policy script reference UTxO."""
        from pycardano import ScriptHash

        script = get_backend().get_script_from_address(
            Address(
                payment_part=ScriptHash(
                    payload=bytes.fromhex(ORDER_NFT_POLICY),
                ),
            ),
        )

        return UTxO(
            input=TransactionInput(
                TransactionId(
                    bytes.fromhex(
                        "1a757d9840dfd77f5aa0223245b553d412328dadb10abc5225f4f8e53ae90ee0",
                    ),
                ),
                index=1,
            ),
            output=TransactionOutput(
                address=Address.decode(script.address),
                amount=Value(coin=22_110_300),
                script=PlutusV2Script(bytes.fromhex(script.script)),
            ),
        )

    def batcher_fee(
        self,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        include_action_fee: bool = True,
    ) -> Assets:
        """Calculate total fee estimate for a mint or burn operation.

        Args:
            in_assets: Input assets (ADA for mints, tokens for burns)
            out_assets: Output assets (tokens for mints, ADA for burns)
            extra_assets: Extra assets (unused, for interface compatibility)
            include_action_fee: If True, include action fee in the calculation

        Returns:
            Total fees in ADA (operator fee + action fee)
        """
        if in_assets.unit() == "lovelace":
            fee_num, fee_den = self.price_ratio(side="mint")
            ada_with_fee = math.ceil(out_assets.quantity() * fee_num / fee_den)
            base_ada = math.ceil(
                out_assets.quantity() * fee_num * 1000 / (fee_den * 1015),
            )
            operator_fee = _calculate_operator_fee(ada_with_fee)
            action_fee = max(0, ada_with_fee - base_ada)
            if include_action_fee:
                return Assets(lovelace=operator_fee + action_fee)
            return Assets(lovelace=operator_fee)

        if in_assets.unit() == self.unit_b:
            fee_num, fee_den = self.price_ratio(side="burn")
            ada_with_fee = (in_assets.quantity() * fee_num) // fee_den
            base_ada = (in_assets.quantity() * fee_num * 1000) // (fee_den * 985)
            operator_fee = _calculate_operator_fee(ada_with_fee)
            action_fee = max(0, base_ada - ada_with_fee)
            if include_action_fee:
                return Assets(lovelace=operator_fee + action_fee)
            return Assets(lovelace=operator_fee)

        raise ValueError(f"Unsupported input asset: {in_assets.unit()}")

    def get_reserve_ratio(self) -> float:
        """Get current reserve ratio as a percentage."""
        liabilities = (
            self._pool_datum.djed_in_circulation * self._oracle_rate.denominator
        )
        if liabilities == 0:
            return float("inf")

        assets = self._pool_datum.ada_in_reserve * self._oracle_rate.numerator
        return (assets / liabilities) * 100

    @staticmethod
    def _fraction_floor_non_negative(value: Fraction) -> int:
        """Convert a rational to a non-negative floored integer."""
        return max(0, value.numerator // value.denominator)

    def max_mintable_djed(self) -> int:
        """Maximum DJED mintable under min reserve ratio constraint."""
        djed_ada_rate = Fraction(
            self._oracle_rate.denominator,
            self._oracle_rate.numerator,
        )
        mint_fee = Fraction(FEE_NUMERATOR, FEE_DENOMINATOR)
        min_reserve_ratio = Fraction(MIN_RESERVE_RATIO_PERCENT, 100)
        denominator_factor = min_reserve_ratio - 1 - mint_fee
        if denominator_factor <= 0:
            return 0

        value = (
            Fraction(self._pool_datum.ada_in_reserve)
            - min_reserve_ratio * self._pool_datum.djed_in_circulation * djed_ada_rate
        ) / (djed_ada_rate * denominator_factor)
        return self._fraction_floor_non_negative(value)

    def max_mintable_shen(self) -> int:
        """Maximum SHEN mintable under max reserve ratio constraint."""
        if self._pool_datum.shen_in_circulation <= 0:
            return 0

        djed_ada_rate = Fraction(
            self._oracle_rate.denominator,
            self._oracle_rate.numerator,
        )
        shen_ada_rate = (
            Fraction(self._pool_datum.ada_in_reserve)
            - self._pool_datum.djed_in_circulation * djed_ada_rate
        ) / self._pool_datum.shen_in_circulation
        if shen_ada_rate <= 0:
            return 0

        mint_fee = Fraction(FEE_NUMERATOR, FEE_DENOMINATOR)
        max_reserve_ratio = Fraction(MAX_RESERVE_RATIO_PERCENT, 100)
        value = (
            (
                max_reserve_ratio * self._pool_datum.djed_in_circulation * djed_ada_rate
                - Fraction(self._pool_datum.ada_in_reserve)
            )
            / shen_ada_rate
            / (1 + mint_fee)
        )

        return max(0, self._fraction_floor_non_negative(value) - 1)

    def max_burnable_shen(self) -> int:
        """Maximum SHEN burnable under min reserve ratio constraint."""
        if self._pool_datum.shen_in_circulation <= 0:
            return 0

        djed_ada_rate = Fraction(
            self._oracle_rate.denominator,
            self._oracle_rate.numerator,
        )
        shen_ada_rate = (
            Fraction(self._pool_datum.ada_in_reserve)
            - self._pool_datum.djed_in_circulation * djed_ada_rate
        ) / self._pool_datum.shen_in_circulation
        if shen_ada_rate <= 0:
            return 0

        burn_fee = Fraction(FEE_NUMERATOR, FEE_DENOMINATOR)
        min_reserve_ratio = Fraction(MIN_RESERVE_RATIO_PERCENT, 100)
        fee_factor = 1 - burn_fee
        if fee_factor <= 0:
            return 0

        value = (
            (
                Fraction(self._pool_datum.ada_in_reserve)
                - min_reserve_ratio
                * self._pool_datum.djed_in_circulation
                * djed_ada_rate
            )
            / shen_ada_rate
            / fee_factor
        )
        return self._fraction_floor_non_negative(value)

    @property
    def swap_forward(self) -> bool:
        """Returns if swap forwarding is enabled."""
        return True

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Get default script class."""
        return PlutusV2Script

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns data class used for handling order datums."""
        return DjedOrderDatum

    @property
    def stake_address(self) -> Address | None:
        """Return the staking address."""
        return None


class DjedOrderBook(DjedShenOrderBookBase):
    """Djed order book for Djed mint/burn operations."""

    @classmethod
    def get_book(
        cls,
        assets: Assets | None = None,
        orders: list[OrderBookOrder] | None = None,
    ) -> "DjedOrderBook":
        """Create a Djed order book snapshot with current on-chain state."""
        if assets is None:
            assets = Assets({"lovelace": 0, DJED_TOKEN: 0})
        buy_orders = orders or []

        ob = DjedOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook([]),
            buy_book_full=BuyOrderBook(buy_orders),
        )

        oracle_utxo, oracle_datum = cls._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()

        ob._oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        ob._pool_datum = pool_datum
        ob._oracle_utxo = oracle_utxo
        ob._pool_utxo = pool_utxo
        ob._minting_policy_ref = cls._get_minting_policy_ref_utxo()
        ob.buy_book_full = ob.buy_book_full[:3]

        return ob

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "Djed"

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob."""
        return "Djed"

    def price_ratio(self, side: str | None = None) -> tuple[int, int]:
        """Return ADA/DJED price as (numerator, denominator).

        Args:
            side: "mint" to add 1.5% fee, "burn" to deduct 1.5% fee,
                  None for the raw oracle rate without fees.
        """
        num = self._oracle_rate.denominator
        den = self._oracle_rate.numerator
        if side == "mint":
            num *= 1015
            den *= 1000
        elif side == "burn":
            num *= 985
            den *= 1000
        elif side is not None:
            raise ValueError("side must be 'mint', 'burn', or None")
        return num, den

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Raw oracle price without fees.

        Returns:
            A `Tuple[Decimal, Decimal]` where the first `Decimal` is the cost
                in ADA (lovelace) to purchase 1 DJED, and the second `Decimal`
                is the cost in DJED to purchase 1 lovelace.
        """
        num, den = self.price_ratio()
        return Decimal(num) / Decimal(den), Decimal(den) / Decimal(num)

    def get_amount_out(
        self,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate output for a given input for Djed mint/burn operations.

        Args:
            asset: Input assets (ADA for mint, DJED for burn)

        Returns:
            Tuple of (output_assets, slippage). Includes 1.5% action fee.
        """
        if asset.unit() == "lovelace":
            num, den = self.price_ratio(side="mint")
            token_out = (asset.quantity() * den) // num
            return Assets(**{self.unit_b: token_out}), 0
        num, den = self.price_ratio(side="burn")
        ada_out = (asset.quantity() * num) // den
        return Assets(lovelace=ada_out), 0

    def get_amount_in(
        self,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate required input for a desired output for Djed mint/burn operations.

        Args:
            asset: Desired output assets (DJED for mint, ADA for burn)

        Returns:
            Tuple of (input_assets, slippage). Includes 1.5% action fee.
        """
        if asset.unit() == "lovelace":
            num, den = self.price_ratio(side="burn")
            token_in = math.ceil(asset.quantity() * den / num)
            return Assets(**{self.unit_b: token_in}), 0
        num, den = self.price_ratio(side="mint")
        ada_in = math.ceil(asset.quantity() * num / den)
        return Assets(lovelace=ada_in), 0

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Create a Djed mint/burn order.

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        target_address = address_target or address_source
        if in_assets.unit() == "lovelace":
            amount = out_assets.quantity()
            is_mint = True
            if out_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum mint amount for {self.dex()} is {MIN_ORDER_AMOUNT}",
                )
        elif in_assets.unit() == self.unit_b:
            amount = in_assets.quantity()
            is_mint = False
            if in_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum burn amount for {self.dex()} is {MIN_ORDER_AMOUNT}",
                )
        else:
            raise ValueError(
                f"Unsupported input asset for {self.dex()}: {in_assets.unit()}",
            )

        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot
        creation_time = _slot_to_posix_ms(ttl_slot)

        reserve_ratio = self.get_reserve_ratio()

        if is_mint:
            if reserve_ratio <= MIN_RESERVE_RATIO_PERCENT:
                raise ValueError(
                    "DJED mint not allowed: reserve ratio "
                    f"{reserve_ratio:.2f}% must be > "
                    f"{MIN_RESERVE_RATIO_PERCENT}%",
                )
            num, den = self.price_ratio(side="mint")
            ada_amount = math.ceil(amount * num / den)
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = ada_amount + self._pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=DjedMintAction(djed_amount=amount, ada_amount=ada_amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=self._oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(lovelace=total_ada)
        else:
            num, den = self.price_ratio(side="burn")
            ada_amount = (amount * num) // den
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = self._pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=DjedBurnAction(djed_amount=amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=self._oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(**{self.unit_b: amount, "lovelace": total_ada})

        tx_builder.reference_inputs.add(self._oracle_utxo)
        tx_builder.reference_inputs.add(self._pool_utxo)
        tx_builder.reference_inputs.add(self._minting_policy_ref)

        order_address = Address.decode(self.order_selector()[0])
        output_assets.root[ORDER_NFT_POLICY + ORDER_NFT_NAME_HEX] = 1
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            target_address,
            self._pool_datum,
            self._minting_policy_ref,
        )

        return order_output, order_datum


class ShenOrderBook(DjedShenOrderBookBase):
    """Shen order book for Shen mint/burn operations."""

    @classmethod
    def get_book(
        cls,
        assets: Assets | None = None,
        orders: list[OrderBookOrder] | None = None,
    ) -> "ShenOrderBook":
        """Create a Shen order book snapshot with current on-chain state."""
        if assets is None:
            assets = Assets({"lovelace": 0, SHEN_TOKEN: 0})
        buy_orders = orders or []

        ob = ShenOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook([]),
            buy_book_full=BuyOrderBook(buy_orders),
        )

        oracle_utxo, oracle_datum = cls._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()

        ob._oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        ob._pool_datum = pool_datum
        ob._oracle_utxo = oracle_utxo
        ob._pool_utxo = pool_utxo
        ob._minting_policy_ref = cls._get_minting_policy_ref_utxo()
        ob.buy_book_full = ob.buy_book_full[:3]

        return ob

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "Shen"

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob."""
        return "Shen"

    def price_ratio(self, side: str | None = None) -> tuple[int, int]:
        """Return ADA/SHEN price as (numerator, denominator).

        Args:
            side: "mint" to add 1.5% fee, "burn" to deduct 1.5% fee,
                  None for the raw rate without fees.
        """
        num = (
            self._pool_datum.ada_in_reserve * self._oracle_rate.numerator
            - self._pool_datum.djed_in_circulation * self._oracle_rate.denominator
        )
        den = self._pool_datum.shen_in_circulation * self._oracle_rate.numerator

        if side == "mint":
            num *= 1015
            den *= 1000
        elif side == "burn":
            num *= 985
            den *= 1000
        elif side is not None:
            raise ValueError("side must be 'mint', 'burn', or None")

        return num, den

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Raw price without fees.

        Returns:
            A `Tuple[Decimal, Decimal]` where the first `Decimal` is the cost
                in ADA (lovelace) to purchase 1 SHEN, and the second `Decimal`
                is the cost in SHEN to purchase 1 lovelace.
        """
        num, den = self.price_ratio()
        return Decimal(num) / Decimal(den), Decimal(den) / Decimal(num)

    def get_amount_out(
        self,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate output for a given input for Shen mint/burn operations.

        Args:
            asset: Input assets (ADA for mint, SHEN for burn)

        Returns:
            Tuple of (output_assets, slippage). Includes 1.5% action fee.
        """
        side = "mint" if asset.unit() == "lovelace" else "burn"
        num, den = self.price_ratio(side=side)
        if num <= 0 or den <= 0:
            return (
                Assets(**{self.unit_b: 0})
                if asset.unit() == "lovelace"
                else Assets(lovelace=0),
                0,
            )
        if asset.unit() == "lovelace":
            token_out = (asset.quantity() * den) // num
            return Assets(**{self.unit_b: token_out}), 0
        ada_out = (asset.quantity() * num) // den
        return Assets(lovelace=ada_out), 0.0

    def get_amount_in(
        self,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate required input for a desired output for Shen mint/burn operations.

        Args:
            asset: Desired output assets (SHEN for mint, ADA for burn)

        Returns:
            Tuple of (input_assets, slippage). Includes 1.5% action fee.
        """
        if asset.unit() == "lovelace":
            num, den = self.price_ratio(side="burn")
            if num <= 0 or den <= 0:
                return Assets(**{self.unit_b: 0}), 0
            token_in = math.ceil(asset.quantity() * den / num)
            return Assets(**{self.unit_b: token_in}), 0
        num, den = self.price_ratio(side="mint")
        if num <= 0 or den <= 0:
            return Assets(lovelace=0), 0
        ada_in = math.ceil(asset.quantity() * num / den)
        return Assets(lovelace=ada_in), 0

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Create a Shen mint/burn order.

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        target_address = address_target or address_source
        if in_assets.unit() == "lovelace":
            amount = out_assets.quantity()
            is_mint = True
            if out_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum mint amount for {self.dex()} is {MIN_ORDER_AMOUNT}",
                )
        elif in_assets.unit() == self.unit_b:
            amount = in_assets.quantity()
            is_mint = False
            if in_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum burn amount for {self.dex()} is {MIN_ORDER_AMOUNT}",
                )
        else:
            raise ValueError(
                f"Unsupported input asset for {self.dex()}: {in_assets.unit()}",
            )

        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot
        creation_time = _slot_to_posix_ms(ttl_slot)

        reserve_ratio = self.get_reserve_ratio()

        if is_mint:
            if reserve_ratio >= MAX_RESERVE_RATIO_PERCENT:
                raise ValueError(
                    "SHEN mint not allowed: reserve ratio "
                    f"{reserve_ratio:.2f}% must be < "
                    f"{MAX_RESERVE_RATIO_PERCENT}%",
                )
            num, den = self.price_ratio(side="mint")
            ada_amount = math.ceil(amount * num / den)
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = ada_amount + self._pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=ShenMintAction(shen_amount=amount, ada_amount=ada_amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=self._oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(lovelace=total_ada)
        else:
            if reserve_ratio <= MIN_RESERVE_RATIO_PERCENT:
                raise ValueError(
                    "SHEN burn not allowed: reserve ratio "
                    f"{reserve_ratio:.2f}% must be > "
                    f"{MIN_RESERVE_RATIO_PERCENT}%",
                )
            num, den = self.price_ratio(side="burn")
            ada_amount = math.ceil(amount * num / den)
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = self._pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=ShenBurnAction(shen_amount=amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=self._oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(**{self.unit_b: amount, "lovelace": total_ada})

        tx_builder.reference_inputs.add(self._oracle_utxo)
        tx_builder.reference_inputs.add(self._pool_utxo)
        tx_builder.reference_inputs.add(self._minting_policy_ref)

        order_address = Address.decode(self.order_selector()[0])
        output_assets.root[ORDER_NFT_POLICY + ORDER_NFT_NAME_HEX] = 1
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            target_address,
            self._pool_datum,
            self._minting_policy_ref,
        )

        return order_output, order_datum
