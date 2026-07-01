"""SaturnSwap Order Book Module.

This module handles the limit order path for SaturnSwap.
"""

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any
from typing import ClassVar
from typing import Union

from pycardano import Address
from pycardano import PaymentSigningKey
from pycardano import PaymentVerificationKey
from pycardano import PlutusData
from pycardano import PlutusV2Script
from pycardano import PlutusV3Script
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import VerificationKeyHash
from pycardano.utils import min_lovelace

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.core.errors import NotAPoolError
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderState
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.utility import asset_to_value

# SaturnSwap charges an on-chain taker fee on every fill that is NOT co-signed by
# its authorized hot-key; co-signed (protocol-routed) fills are exempt via the
# validator's ``fees_paid_or_auth``. In 2026-06 the protocol redeployed the swap
# contract with the non-auth fee lowered 4% -> 1% (validator ``fee_percent``
# 400 -> 100); only that integer changed, so the contract kept the same stake
# credential but gained a new script hash/address. Dendrite tracks both the live
# 1% contract and the legacy 4% contract so in-flight legacy orders still fill at
# the correct rate (see SaturnSwapOrderState / SaturnSwapLegacyOrderState).
SATURNSWAP_TAKER_FEE_BPS = 100
SATURNSWAP_LEGACY_TAKER_FEE_BPS = 400

# Live 1% contract; script hash
# 73990b71041ceade6f867617f6ce9f187ab710ea2bf1ff8db7d0292f.
SATURNSWAP_ORDER_ADDRESS = (
    "addr1z9eejzm3qsww4hn0semp0akwnuv84dcsag4lrludklgzjt"
    "675jq4yvpskgayj55xegdp30g5rfynax66r8vgn9fldndsrfnae7"
)
# Legacy 4% contract (pre-2026-06 deployment).
SATURNSWAP_LEGACY_ORDER_ADDRESS = (
    "addr1zyd0sj57d9lpu7cy9g9qdurpazqc9l4eaxk6j59nd2gkh4"
    "275jq4yvpskgayj55xegdp30g5rfynax66r8vgn9fldndsqzf5tn"
)
# V3 (PlutusV3) contract; script hash
# 6023f59dce0064f1d6d27594dbea25bc4305a9f6a10f3a064037553a.
SATURNSWAP_V3_ORDER_ADDRESS = (
    "addr1z9sz8avaecqxfuwk6f6efkl2yk7yxpdf76ss7wsxgqm42w"
    "h2l9cdyhc0eja9mxq0lgeer90edhlfymnxv2ym3szcetqsp0ume8"
)

# When the protocol shares its authorize hot-key, set this env var to the signing
# key and dendrite builds the fee-free authorized fill on either contract.
SATURNSWAP_AUTHORIZE_KEY_ENV = "SATURNSWAP_AUTHORIZE_KEY"


def _load_authorize_signing_key(raw: str) -> PaymentSigningKey:
    """Load the authorize signing key from CBOR-hex or raw 32-byte hex."""
    raw = raw.strip()
    try:
        return PaymentSigningKey.from_cbor(raw)
    except (ValueError, TypeError, KeyError):
        return PaymentSigningKey(bytes.fromhex(raw))


def saturnswap_authorize_signing_key() -> PaymentSigningKey | None:
    """Return the SaturnSwap authorize signing key from the environment.

    Set ``SATURNSWAP_AUTHORIZE_KEY`` (the hot-key as ``sk.to_cbor_hex()`` or raw
    32-byte hex) to build fee-free authorized fills. ``swap_utxo`` then drops the
    taker-fee output and adds the key's hash as a required signer. The CALLER must
    add this key when signing the transaction (e.g. ``build_and_sign([…, key])``).
    Returns ``None`` (default behaviour, on-chain taker fee) when the env var is
    unset.
    """
    raw = os.environ.get(SATURNSWAP_AUTHORIZE_KEY_ENV)
    return _load_authorize_signing_key(raw) if raw else None


@lru_cache(maxsize=8)
def _vkey_hash_for(raw: str) -> VerificationKeyHash:
    """Verification-key hash for an authorize signing key (cached by value)."""
    return PaymentVerificationKey.from_signing_key(
        _load_authorize_signing_key(raw),
    ).hash()


def _authorize_vkey_hash() -> VerificationKeyHash | None:
    """Verification-key hash of the configured authorize signing key (or None)."""
    raw = os.environ.get(SATURNSWAP_AUTHORIZE_KEY_ENV)
    return _vkey_hash_for(raw.strip()) if raw else None


@dataclass
class SaturnSwapSomeInt(PlutusData):
    """Some(Int) wrapper for Option<Int>."""

    CONSTR_ID = 0
    value: int


@dataclass
class SaturnSwapOutputReference(PlutusData):
    """Output reference used for double-satisfaction protection."""

    CONSTR_ID = 0
    tx_id: "SaturnSwapTxId"
    index: int


@dataclass
class SaturnSwapTxId(PlutusData):
    """TxId wrapper used in OutputReference."""

    CONSTR_ID = 0
    value: bytes


@dataclass
class SaturnSwapPaymentDatum(PlutusData):
    """PaymentDatum { output_reference }."""

    CONSTR_ID = 0
    output_reference: SaturnSwapOutputReference


@dataclass
class SaturnSwapSwapDatum(OrderDatum):
    """SwapDatum.

    Fields:
        owner
        policy_id_sell
        asset_name_sell
        amount_sell
        policy_id_buy
        asset_name_buy
        amount_buy
        valid_before_time (Option<Int>)
        output_reference
    """

    CONSTR_ID = 0
    owner: PlutusFullAddress
    policy_id_sell: bytes
    asset_name_sell: bytes
    amount_sell: int
    policy_id_buy: bytes
    asset_name_buy: bytes
    amount_buy: int
    valid_before_time: Union[PlutusNone, SaturnSwapSomeInt]
    output_reference: SaturnSwapOutputReference

    def pool_pair(self) -> Assets | None:
        """Return the asset pair for this swap datum."""
        sell_unit = (
            "lovelace"
            if self.policy_id_sell == b""
            else self.policy_id_sell.hex() + self.asset_name_sell.hex()
        )
        buy_unit = (
            "lovelace"
            if self.policy_id_buy == b""
            else self.policy_id_buy.hex() + self.asset_name_buy.hex()
        )
        return Assets(**{sell_unit: 0}) + Assets(**{buy_unit: 0})

    def address_source(self) -> str | None:
        """Return the maker address as a bech32 string."""
        return self.owner.to_address().encode()

    def requested_amount(self) -> Assets:
        """Return the requested buy asset amount."""
        # Maker is requesting buy asset in the amount of amount_buy
        buy_unit = (
            "lovelace"
            if self.policy_id_buy == b""
            else self.policy_id_buy.hex() + self.asset_name_buy.hex()
        )
        return Assets(**{buy_unit: self.amount_buy})

    def order_type(self) -> OrderType | None:
        """Return the order type classification."""
        return OrderType.swap


@dataclass
class SaturnSwapOutputReferenceV3(PlutusData):
    """Flat OutputReference: Constr0[tx_id: bytes(32), index]. No TxId wrapper."""

    CONSTR_ID = 0
    tx_id: bytes
    index: int


@dataclass
class SaturnSwapPaymentDatumV3(PlutusData):
    """PaymentDatum { output_reference } with the flat V3 OutputReference."""

    CONSTR_ID = 0
    output_reference: SaturnSwapOutputReferenceV3


@dataclass
class SaturnSwapCoverage(PlutusData):
    """Aegis coverage { vault, premium_bps, policy_ref }."""

    CONSTR_ID = 0
    vault: PlutusFullAddress
    premium_bps: int
    policy_ref: SaturnSwapOutputReferenceV3


@dataclass
class SaturnSwapSomeCoverage(PlutusData):
    """Some(Coverage) wrapper for Option<Coverage>."""

    CONSTR_ID = 0
    value: SaturnSwapCoverage


@dataclass
class SaturnSwapSwapDatumV3(OrderDatum):
    """V3 SwapDatum (11 fields).

    Extends the V2 layout with ``min_partial_fill`` and optional Aegis
    ``coverage``, and uses the flat :class:`SaturnSwapOutputReferenceV3`.
    """

    CONSTR_ID = 0
    owner: PlutusFullAddress
    policy_id_sell: bytes
    asset_name_sell: bytes
    amount_sell: int
    policy_id_buy: bytes
    asset_name_buy: bytes
    amount_buy: int
    valid_before_time: Union[PlutusNone, SaturnSwapSomeInt]
    output_reference: SaturnSwapOutputReferenceV3
    min_partial_fill: int
    coverage: Union[SaturnSwapSomeCoverage, PlutusNone]

    def pool_pair(self) -> Assets | None:
        """Return the asset pair for this swap datum."""
        sell_unit = (
            "lovelace"
            if self.policy_id_sell == b""
            else self.policy_id_sell.hex() + self.asset_name_sell.hex()
        )
        buy_unit = (
            "lovelace"
            if self.policy_id_buy == b""
            else self.policy_id_buy.hex() + self.asset_name_buy.hex()
        )
        return Assets(**{sell_unit: 0}) + Assets(**{buy_unit: 0})

    def address_source(self) -> str | None:
        """Return the maker address as a bech32 string."""
        return self.owner.to_address().encode()

    def requested_amount(self) -> Assets:
        """Return the requested buy asset amount."""
        buy_unit = (
            "lovelace"
            if self.policy_id_buy == b""
            else self.policy_id_buy.hex() + self.asset_name_buy.hex()
        )
        return Assets(**{buy_unit: self.amount_buy})

    def order_type(self) -> OrderType | None:
        """Return the order type classification."""
        return OrderType.swap

    def is_covered(self) -> bool:
        """Return whether the order carries Aegis coverage."""
        return isinstance(self.coverage, SaturnSwapSomeCoverage)

    def premium_bps(self) -> int | None:
        """Coverage premium in basis points, or None when uncovered."""
        return self.coverage.value.premium_bps if self.is_covered() else None

    def coverage_vault(self) -> str | None:
        """Aegis vault bech32 address, or None when uncovered."""
        if not self.is_covered():
            return None
        return self.coverage.value.vault.to_address().encode()

    def premium_for_fill(self, user_sell_amount: int) -> int:
        """Out-of-pocket premium (buy asset) for a fill of ``user_sell_amount``.

        ``max(1, user_sell_amount * premium_bps // 10000)`` for covered orders;
        ``0`` when uncovered.
        """
        if not self.is_covered():
            return 0
        base = (user_sell_amount * self.coverage.value.premium_bps) // 10_000
        return max(1, base)


@dataclass
class SaturnSwapSwapAction(PlutusData):
    """SwapAction(user_sell_amount, input_index, output_index)."""

    CONSTR_ID = 0
    user_sell_amount: int
    input_index: int
    output_index: int

    def set_idx(
        self,
        tx_builder: TransactionBuilder,
    ) -> None:
        """Set the input/output indices from the built transaction.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        owner_address = None
        input_utxo = None

        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.input_index = key.index
                input_utxo = tx_builder.inputs[self.input_index]
                break
        else:
            raise RuntimeError("Swap input UTxO not found")

        for i, tx_in in enumerate(tx_builder.inputs):
            if (
                tx_in.input.transaction_id == input_utxo.input.transaction_id
                and tx_in.input.index == input_utxo.input.index
            ):
                self.input_index = i
                break

        swap_datum = tx_builder.datums[input_utxo.output.datum_hash]
        owner_address = swap_datum.owner.to_address()

        input_tx_id = input_utxo.input.transaction_id
        input_index = input_utxo.input.index

        for i, txo in enumerate(tx_builder.outputs):
            if not isinstance(txo.datum, SaturnSwapPaymentDatum):
                continue
            output_ref = txo.datum.output_reference
            if (
                output_ref.tx_id.value == bytes.fromhex(str(input_tx_id))
                and output_ref.index == input_index
                and txo.address == owner_address
            ):
                self.output_index = i
                return


@dataclass
class SaturnSwapCancelAction(PlutusData):
    """CancelAction(input_index)."""

    CONSTR_ID = 1
    input_index: int

    def set_idx(self, tx_builder: TransactionBuilder) -> None:
        """Set the input index from the built transaction.

        It is necessary to make sure that redeemer indices are already set before
        calling this function.
        """
        for key, value in tx_builder.redeemers().items():
            if value.data == self:
                self.input_index = key.index
                return


class _SaturnSwapOrderStateBase(AbstractOrderState):
    """Shared SaturnSwap order-state logic for both contract versions.

    Concrete subclasses pin the contract-specific bits — the order script
    ``order_selector()`` address and the non-auth ``TAKER_FEE_BPS`` — and supply
    ``dex()``. The base intentionally leaves ``dex()`` unimplemented so the
    subclass-discovery walk skips it and only registers the concrete leaves.
    """

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False

    # Non-auth taker fee in basis points; set by each concrete contract class.
    TAKER_FEE_BPS: ClassVar[int]

    _batcher: Assets = Assets(lovelace=0)
    _datum_parsed: PlutusData | None = None

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """SaturnSwap uses parameterized scripts, no global dex NFT."""
        return None

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return pool selector for order UTxOs."""
        return PoolSelector(addresses=cls.order_selector())

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Return datum class used for orders."""
        return SaturnSwapSwapDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV2Script] | type[PlutusV3Script]:
        """Return default script type (V2 base; the V3 leaf overrides)."""
        return PlutusV2Script

    @property
    def price(self) -> tuple[int, int]:
        """Price as (numerator, denominator) for in_unit/out_unit."""
        amount_sell = int(self.order_datum.amount_sell)
        amount_buy = int(self.order_datum.amount_buy)
        if amount_sell == 0 or amount_buy == 0:
            return (0, 0)
        return (amount_buy, amount_sell)

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Estimate output assets for a given input."""
        if len(asset) != 1:
            raise ValueError("Input asset must contain exactly one unit.")
        if asset.unit() != self.in_unit:
            raise ValueError("Input asset unit must match in_unit.")

        num, denom = self.price
        fee_bps = self.volume_fee

        in_qty = int(asset.quantity())
        out_qty = (in_qty * denom + num - 1) // num
        fee = (out_qty * fee_bps) // 10_000
        out_qty = min(out_qty - fee, int(self.order_datum.amount_sell))
        return Assets(**{self.out_unit: int(out_qty)}), 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Estimate input assets for a desired output."""
        if len(asset) != 1:
            raise ValueError("Output asset must contain exactly one unit.")
        if asset.unit() != self.out_unit:
            raise ValueError("Output asset unit must match out_unit.")

        num, denom = self.price
        fee_bps = self.volume_fee

        desired_out = int(asset.quantity())
        desired_out = min(desired_out, int(self.order_datum.amount_sell))
        gross_out = (desired_out * 10_000 + (10_000 - fee_bps) - 1) // (
            10_000 - fee_bps
        )
        in_qty = (gross_out * num + denom - 1) // denom
        return Assets(**{self.in_unit: int(in_qty)}), 0

    @property
    def available(self) -> Assets:
        """Return available output asset amount."""
        return Assets(**{self.out_unit: self.order_datum.amount_sell})

    @property
    def volume_fee(self) -> int:
        """Taker fee in basis points (0 when the authorize hot-key co-signs).

        SaturnSwap waives the on-chain taker fee for fills co-signed by its
        authorized hot-key, so the fee is 0 when ``SATURNSWAP_AUTHORIZE_KEY`` is
        configured (see :func:`saturnswap_authorize_signing_key`); otherwise it
        is the contract-specific ``TAKER_FEE_BPS``.
        """
        return 0 if _authorize_vkey_hash() is not None else self.TAKER_FEE_BPS

    @property
    def swap_forward(self) -> bool:
        """Return whether swaps are forward."""
        return True

    @property
    def stake_address(self) -> Address | None:
        """Return staking address if applicable."""
        return None

    @property
    def tvl(self) -> int:
        """Return total value locked for the order."""
        return int(self.available.quantity())

    @property
    def reference_utxo(self) -> UTxO | None:
        """Get the script reference UTxO (if available)."""
        script_info = get_backend().get_script_from_address(
            Address.decode(self.order_selector()[0]),
        )

        if script_info is None or script_info.script is None:
            return None

        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script_info.tx_hash)),
                index=script_info.tx_index,
            ),
            output=TransactionOutput(
                address=script_info.address,
                amount=asset_to_value(script_info.assets),
                script=self.default_script_class()(bytes.fromhex(script_info.script)),
            ),
        )

    @property
    def pool_id(self) -> str:
        """Return identifier for the order pool."""
        datum = self.order_datum
        return datum.policy_id_sell.hex() + datum.asset_name_sell.hex()

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Set inactive flag based on datum fields."""
        datum = cls.order_datum_class().from_cbor(values["datum_cbor"])

        buy_unit = (
            "lovelace"
            if datum.policy_id_buy == b""
            else datum.policy_id_buy.hex() + datum.asset_name_buy.hex()
        )
        sell_unit = (
            "lovelace"
            if datum.policy_id_sell == b""
            else datum.policy_id_sell.hex() + datum.asset_name_sell.hex()
        )
        if values["assets"].unit() != buy_unit:
            quantity = values["assets"].root.pop(sell_unit)
            values["assets"].root[sell_unit] = quantity

        # If expiration exists, mark inactive when expired.
        vbt = datum.valid_before_time
        if isinstance(vbt, SaturnSwapSomeInt):
            import time

            now_ms = int(time.time() * 1000)
            if now_ms >= vbt.value:
                values["inactive"] = True

        # If amounts are zero, treat as inactive.
        if datum.amount_sell == 0 or datum.amount_buy == 0:
            values["inactive"] = True

        return values

    def _add_fee_or_authorize(
        self,
        tx_builder: TransactionBuilder,
        sell_unit: str,
        new_amount_sell: int,
        payment_datum: "SaturnSwapPaymentDatum",
    ) -> None:
        """Add the taker-fee output, or require the authorize hot-key signature.

        SaturnSwap exempts fills co-signed by its authorized hot-key from the
        on-chain fee (``fees_paid_or_auth``). When ``SATURNSWAP_AUTHORIZE_KEY`` is
        set, drop the fee output and add the hot-key's hash as a required signer
        so ``tx_signed_by_authority`` passes (the caller signs with the matching
        key from :func:`saturnswap_authorize_signing_key`).
        """
        auth_vkey_hash = _authorize_vkey_hash()
        if auth_vkey_hash is not None:
            if tx_builder.required_signers is None:
                tx_builder.required_signers = []
            if auth_vkey_hash not in tx_builder.required_signers:
                tx_builder.required_signers.append(auth_vkey_hash)
            return
        fee_address = Address.decode(
            "addr1q8x4rlqhrq4rhqhnkamw3fdqmzqgum79yragg4gptcjpph"
            "mrc2rpt0exfch4s47fu32amr45vh9wg053hmcx9k7kkcrq6kxftd",
        )
        fee_amount = (new_amount_sell * self.volume_fee) // 10_000
        fee_assets = Assets(**{sell_unit: fee_amount})
        fee_output = TransactionOutput(
            address=fee_address,
            amount=asset_to_value(fee_assets),
            datum=payment_datum,
        )
        fee_output.amount.coin = max(
            fee_output.amount.coin,
            min_lovelace(tx_builder.context, output=fee_output),
        )
        tx_builder.add_output(fee_output)

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
        """Build swap transaction outputs and redeemer for this order."""
        two_ada = 2_000_000

        if self.reference_utxo is not None:
            tx_builder.reference_inputs.add(self.reference_utxo)

        order_address = (
            get_backend()
            .get_pool_in_tx(
                self.tx_hash,
                addresses=self.pool_selector().addresses,
            )[0]
            .address
        )

        input_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=order_address,
                amount=asset_to_value(self.assets),
                datum_hash=self.order_datum.hash(),
            ),
        )
        script_input_lovelace = input_utxo.output.amount.coin

        # Build output reference for payment datum
        output_ref = SaturnSwapOutputReference(
            tx_id=SaturnSwapTxId(value=bytes.fromhex(self.tx_hash)),
            index=self.tx_index,
        )
        payment_datum = SaturnSwapPaymentDatum(output_reference=output_ref)

        user_sell_amount = int(in_assets.quantity())

        # Determine partial fill
        partial = user_sell_amount < self.order_datum.amount_buy

        sell_unit = out_assets.unit()
        buy_unit = in_assets.unit()
        sell_is_ada = sell_unit == "lovelace"

        # Owner payment output (buy asset)
        owner_address = self.order_datum.owner.to_address()
        owner_assets = Assets(**{buy_unit: user_sell_amount})
        new_amount_sell = _ratio_amount(
            self.order_datum.amount_buy,
            user_sell_amount,
            self.order_datum.amount_sell,
        )
        if partial and sell_is_ada and new_amount_sell > two_ada:
            owner_assets.root["lovelace"] = (
                owner_assets.root.get("lovelace", 0) + two_ada
            )
        elif not partial and not sell_is_ada:
            # Full fill when maker is selling a token:
            # owner must receive buy amount plus original script lovelace.
            owner_assets.root["lovelace"] = (
                owner_assets.root.get("lovelace", 0) + script_input_lovelace
            )

        owner_output = TransactionOutput(
            address=owner_address,
            amount=asset_to_value(owner_assets),
            datum=payment_datum,
        )
        owner_output.amount.coin = max(
            owner_output.amount.coin,
            min_lovelace(tx_builder.context, output=owner_output),
        )
        tx_builder.add_output(owner_output)

        # Taker-fee output for unauthorized fills, or require the hot-key
        # signature for authorized (fee-free) fills.
        self._add_fee_or_authorize(
            tx_builder,
            sell_unit,
            new_amount_sell,
            payment_datum,
        )

        # Redeemer uses input and owner-output indices.
        action = SaturnSwapSwapAction(
            user_sell_amount=user_sell_amount,
            input_index=0,
            output_index=0,
        )
        redeemer = Redeemer(action)

        tx_builder.add_script_input(
            utxo=input_utxo,
            script=self.reference_utxo,
            redeemer=redeemer,
        )
        tx_builder.datums.update({self.order_datum.hash(): self.order_datum})

        # Partial fill: create new swap output back to script with updated datum
        if partial:
            new_amount_buy = self.order_datum.amount_buy - user_sell_amount
            new_amount_sell = _ratio_amount(
                self.order_datum.amount_buy,
                new_amount_buy,
                self.order_datum.amount_sell,
            )

            corrected_new_amount_sell = new_amount_sell
            corrected_new_amount_buy = new_amount_buy
            if sell_is_ada and new_amount_sell > two_ada:
                corrected_new_amount_sell = new_amount_sell - two_ada
                corrected_new_amount_buy = _ratio_amount(
                    self.order_datum.amount_sell,
                    corrected_new_amount_sell,
                    self.order_datum.amount_buy,
                )

            new_datum = SaturnSwapSwapDatum(
                owner=self.order_datum.owner,
                policy_id_sell=self.order_datum.policy_id_sell,
                asset_name_sell=self.order_datum.asset_name_sell,
                amount_sell=corrected_new_amount_sell,
                policy_id_buy=self.order_datum.policy_id_buy,
                asset_name_buy=self.order_datum.asset_name_buy,
                amount_buy=corrected_new_amount_buy,
                valid_before_time=self.order_datum.valid_before_time,
                output_reference=output_ref,
            )

            residual_assets = Assets(**{sell_unit: corrected_new_amount_sell})
            residual_output = TransactionOutput(
                address=order_address,
                amount=asset_to_value(residual_assets),
                datum=new_datum,
            )
            if sell_is_ada:
                residual_output.amount.coin = max(
                    residual_output.amount.coin,
                    min_lovelace(tx_builder.context, output=residual_output),
                )
            else:
                residual_output.amount.coin = max(
                    script_input_lovelace,
                    min_lovelace(tx_builder.context, output=residual_output),
                )
            tx_builder.datums.update({new_datum.hash(): new_datum})
            return residual_output, new_datum

        return None, self.order_datum


class SaturnSwapOrderState(_SaturnSwapOrderStateBase):
    """Live SaturnSwap order state — current 1% taker-fee contract.

    Orders resting at :data:`SATURNSWAP_ORDER_ADDRESS` (script hash
    ``73990b71…``). New maker orders are created here.
    """

    TAKER_FEE_BPS: ClassVar[int] = SATURNSWAP_TAKER_FEE_BPS

    @classmethod
    def dex(cls) -> str:
        """Return the DEX name."""
        return "SaturnSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return order script addresses (live 1% contract)."""
        return [SATURNSWAP_ORDER_ADDRESS]


class SaturnSwapLegacyOrderState(_SaturnSwapOrderStateBase):
    """Legacy SaturnSwap order state — pre-2026-06 4% taker-fee contract.

    Orders resting at :data:`SATURNSWAP_LEGACY_ORDER_ADDRESS`. Kept so in-flight
    orders on the old contract still fill at their correct 4% fee.
    """

    TAKER_FEE_BPS: ClassVar[int] = SATURNSWAP_LEGACY_TAKER_FEE_BPS

    @classmethod
    def dex(cls) -> str:
        """Return the DEX name."""
        return "SaturnSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return order script addresses (legacy 4% contract)."""
        return [SATURNSWAP_LEGACY_ORDER_ADDRESS]


class SaturnSwapV3OrderState(_SaturnSwapOrderStateBase):
    """V3 (PlutusV3) SaturnSwap order state — 1% taker-fee contract.

    Orders resting at :data:`SATURNSWAP_V3_ORDER_ADDRESS` (script hash
    ``6023f59d…``). Same 1% non-auth taker fee as the live V2 contract; the datum
    is the 11-field :class:`SaturnSwapSwapDatumV3` (flat OutputReference +
    ``min_partial_fill`` + optional coverage) and the script is PlutusV3.
    """

    TAKER_FEE_BPS: ClassVar[int] = SATURNSWAP_TAKER_FEE_BPS

    @classmethod
    def dex(cls) -> str:
        """Return the DEX name."""
        return "SaturnSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return order script addresses (V3 contract)."""
        return [SATURNSWAP_V3_ORDER_ADDRESS]

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Return the V3 order datum class."""
        return SaturnSwapSwapDatumV3

    @classmethod
    def default_script_class(cls) -> type[PlutusV3Script]:
        """V3 orders are spent via a PlutusV3 reference script."""
        return PlutusV3Script


# Concrete SaturnSwap contracts, newest first. The order book walks all of these
# so the V3, live 1%, and legacy 4% contracts are aggregated.
_SATURNSWAP_ORDER_STATE_CLASSES: list[type[_SaturnSwapOrderStateBase]] = [
    SaturnSwapV3OrderState,
    SaturnSwapOrderState,
    SaturnSwapLegacyOrderState,
]


class SaturnSwapOrderBook(AbstractOrderBookState):
    """SaturnSwap order book aggregating individual orders.

    Aggregates orders from every contract version in
    :data:`_SATURNSWAP_ORDER_STATE_CLASSES` (live 1% + legacy 4%).
    """

    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def get_book(
        cls,
        assets: Assets,
        orders: list[_SaturnSwapOrderStateBase] | None = None,
    ) -> "SaturnSwapOrderBook":
        """Build an order book from provided orders or backend UTxOs.

        When ``orders`` is not supplied, UTxOs are fetched for every contract
        version (live 1% + legacy 4%) and validated with the matching order-state
        class so each order carries the correct taker fee.
        """
        min_pair_assets = 2
        utxo_limit = 10_000
        if orders is None:
            # Skip invalid/non-order datums that fail deserialization
            orders = []
            for state_cls in _SATURNSWAP_ORDER_STATE_CLASSES:
                selector = state_cls.pool_selector()
                result = get_backend().get_pool_utxos(
                    limit=utxo_limit,
                    historical=False,
                    **selector.model_dump(),
                )
                for r in result:
                    try:
                        orders.append(state_cls.model_validate(r.model_dump()))
                    except NotAPoolError:
                        continue

        buy_orders: list[OrderBookOrder] = []
        sell_orders: list[OrderBookOrder] = []
        for order in orders:
            if len(order.assets) < min_pair_assets:
                datum = order.order_datum
                buy_unit = (
                    "lovelace"
                    if datum.policy_id_buy == b""
                    else datum.policy_id_buy.hex() + datum.asset_name_buy.hex()
                )
                sell_unit = (
                    "lovelace"
                    if datum.policy_id_sell == b""
                    else datum.policy_id_sell.hex() + datum.asset_name_sell.hex()
                )
                order.assets = Assets(**{buy_unit: 0}) + Assets(**{sell_unit: 0})
            if order.inactive:
                continue
            price_a, price_b = order.price
            if price_a == 0 or price_b == 0:
                continue
            if order.in_unit == assets.unit() and order.out_unit == assets.unit(1):
                price = float(price_a)
                side = sell_orders
            elif order.in_unit == assets.unit(1) and order.out_unit == assets.unit(0):
                price = float(price_b)
                side = buy_orders
            else:
                continue
            o = OrderBookOrder(
                price=price,
                quantity=int(order.available.quantity()),
                state=order,
            )
            side.append(o)

        return SaturnSwapOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook(sell_orders),
            buy_book_full=BuyOrderBook(buy_orders),
        )

    @classmethod
    def dex(cls) -> str:
        """Return the DEX name."""
        return "SaturnSwap"

    @classmethod
    def order_selector(cls) -> list[str]:
        """Return order script addresses for all contract versions."""
        return [
            address
            for state_cls in _SATURNSWAP_ORDER_STATE_CLASSES
            for address in state_cls.order_selector()
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Return pool selector for order UTxOs across all contract versions."""
        return PoolSelector(addresses=cls.order_selector())

    @classmethod
    def default_script_class(cls) -> type[PlutusV2Script]:
        """Return default script type."""
        return SaturnSwapOrderState.default_script_class()

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Return datum class used for orders."""
        return SaturnSwapOrderState.order_datum_class()

    @property
    def swap_forward(self) -> bool:
        """Return whether swaps are forward."""
        return False

    @property
    def stake_address(self) -> Address | None:
        """Return staking address if applicable."""
        return None

    @property
    def pool_id(self) -> str:
        """Return identifier for the order book."""
        return "SaturnSwap"

    @property
    def price(self) -> tuple[Decimal, Decimal]:
        """Return mid price of assets based on the full order books."""
        if not self.buy_book_full or not self.sell_book_full:
            return Decimal(0), Decimal(0)
        buy = Decimal(self.buy_book_full[0].price)
        sell = Decimal(self.sell_book_full[0].price)
        return (
            Decimal((buy + (Decimal(1) / sell)) / 2),
            Decimal((sell + (Decimal(1) / buy)) / 2),
        )

    @property
    def tvl(self) -> Decimal:
        """Return total value locked for the order book."""
        if not self.buy_book_full or not self.sell_book_full:
            return Decimal(0)
        tvl = sum(b.quantity / b.price for b in self.buy_book_full) + sum(
            s.quantity * s.price for s in self.sell_book_full
        )
        return Decimal(int(tvl) / 10**6)

    def get_amount_out(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = False,
    ) -> tuple[Assets, float]:
        """Get the amount of token output for the given input.

        SaturnSwap applies taker fees on output per-order, so fee handling is
        delegated to each order state.
        """
        if len(asset) != 1:
            raise ValueError("Asset should only have one token.")
        if asset.unit() not in [self.unit_a, self.unit_b]:
            raise ValueError(
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}",
            )

        if asset.unit() == self.unit_a:
            book = self.sell_book_full
            unit_out = self.unit_b
        else:
            book = self.buy_book_full
            unit_out = self.unit_a

        in_remaining = Assets(**{asset.unit(): int(asset.quantity())})
        out_assets = Assets(**{unit_out: 0})

        for order in book:
            state = order.state
            if state is None:
                continue

            order_out, _ = state.get_amount_out(in_remaining, precise=precise)
            order_in, _ = state.get_amount_in(order_out, precise=precise)

            if order_out.quantity() <= 0 or order_in.quantity() <= 0:
                break

            out_assets += order_out
            in_remaining -= order_in
            if in_remaining.quantity() <= 0:
                break

        return out_assets, 0

    def get_amount_in(
        self,
        asset: Assets,
        precise: bool = True,
        apply_fee: bool = False,
    ) -> tuple[Assets, float]:
        """Get the amount of token input for the given output.

        SaturnSwap applies taker fees on output per-order, so fee handling is
        delegated to each order state.
        """
        if len(asset) != 1:
            raise ValueError("Asset should only have one token.")
        if asset.unit() not in [self.unit_a, self.unit_b]:
            raise ValueError(
                f"Asset {asset.unit()} is invalid for pool {self.unit_a}-{self.unit_b}",
            )

        if asset.unit() == self.unit_b:
            book = self.sell_book_full
            unit_in = self.unit_a
            unit_out = self.unit_b
        else:
            book = self.buy_book_full
            unit_in = self.unit_b
            unit_out = self.unit_a

        out_remaining = Assets(**{unit_out: int(asset.quantity())})
        in_assets = Assets(**{unit_in: 0})

        for order in book:
            state = order.state
            if state is None:
                continue

            max_out = state.available.quantity()
            take_out = min(out_remaining.quantity(), max_out)
            if take_out <= 0:
                continue

            order_out = Assets(**{unit_out: int(take_out)})
            order_in, _ = state.get_amount_in(order_out, precise=precise)
            if order_in.quantity() <= 0:
                break

            in_assets += order_in
            out_remaining -= order_out
            if out_remaining.quantity() <= 0:
                break

        return in_assets, 0

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
        """Build a transaction by walking the order book."""
        if in_assets.unit() == self.assets.unit():
            book = self.sell_book_full
        else:
            book = self.buy_book_full

        in_remaining = Assets.model_validate(in_assets.model_dump())
        txo = None
        datum = None

        for order in book:
            state = order.state
            if state is None:
                continue

            order_out, _ = state.get_amount_out(in_remaining)
            order_in, _ = state.get_amount_in(order_out)

            if order_out.quantity() <= 0 or order_in.quantity() <= 0:
                break

            txo, datum = state.swap_utxo(
                address_source=address_source,
                in_assets=order_in,
                out_assets=order_out,
                tx_builder=tx_builder,
            )

            in_remaining -= order_in
            if in_remaining.quantity() <= 0:
                break

        return txo, datum


def _ratio_amount(old_token_amount: int, new_token_amount: int, old_amount: int) -> int:
    """Ratio math using ceil rounding."""
    if old_token_amount == 0:
        return 0
    scale = 1_000_000_000_000
    ratio = (new_token_amount * scale + old_token_amount - 1) // old_token_amount
    return (old_amount * ratio + scale - 1) // scale
