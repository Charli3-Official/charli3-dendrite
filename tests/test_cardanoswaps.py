"""Parse tests for Cardano-Swaps v2 one-way swaps.

Cardano-Swaps v2 one-way swap UTxOs are batcher-less, fee-less p2p orders that
offer one asset and ask for another at a fixed Rational price (ask-per-offer).
These tests cover datum round-trip and parsing a ``CardanoSwapsOrderState`` from
a synthetic UTxO ``values`` dict, asserting taker orientation (in=ask, out=offer)
and that beacon tokens are stripped from the tradable balance.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import cbor2
import pytest
from pycardano import Address
from pycardano import Network
from pycardano import PlutusV2Script
from pycardano import ProtocolParameters
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import VerificationKeyHash
from pycardano import plutus_script_hash
from pycardano.backend.base import ChainContext
from pycardano.utils import min_lovelace

from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dexs.ob.cardanoswaps import BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps import BEACON_POLICY_SCRIPT_HEX
from charli3_dendrite.dexs.ob.cardanoswaps import SWAP_VALIDATOR_HASH
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderState
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOutputReference
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsRational
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeInt
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRef
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSwapDatum
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsTxId
from charli3_dendrite.dexs.ob.cardanoswaps import CreateOrCloseSwaps
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithMint
from charli3_dendrite.dexs.ob.cardanoswaps import Swap
from charli3_dendrite.dexs.ob.cardanoswaps import ask_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps import offer_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps import pair_beacon_name
from charli3_dendrite.utility import apply_params_to_script
from charli3_dendrite.utility import asset_to_value

# --- synthetic on-chain values --------------------------------------------

TOKEN_A_POLICY = "a" * 56  # 28-byte policy id, hex
TOKEN_A_NAME = "414141"  # "AAA"
TOKEN_A_UNIT = TOKEN_A_POLICY + TOKEN_A_NAME

TOKEN_B_POLICY = "b" * 56
TOKEN_B_NAME = "424242"  # "BBB"
TOKEN_B_UNIT = TOKEN_B_POLICY + TOKEN_B_NAME

TX_HASH = "c" * 64


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _pair_beacon(offer_id: bytes, offer_name: bytes, ask_id: bytes, ask_name: bytes):
    """sha2_256(a1_id ++ offer_name ++ a2_id ++ ask_name), ADA policy -> 0x00."""
    a1 = offer_id if offer_id != b"" else b"\x00"
    a2 = ask_id if ask_id != b"" else b"\x00"
    return _sha256(a1 + offer_name + a2 + ask_name)


def _offer_beacon(offer_id: bytes, offer_name: bytes) -> bytes:
    """sha2_256(0x01 ++ offer_id ++ offer_name)."""
    return _sha256(b"\x01" + offer_id + offer_name)


def _ask_beacon(ask_id: bytes, ask_name: bytes) -> bytes:
    """sha2_256(0x02 ++ ask_id ++ ask_name)."""
    return _sha256(b"\x02" + ask_id + ask_name)


def _make_datum(
    offer_id: bytes,
    offer_name: bytes,
    ask_id: bytes,
    ask_name: bytes,
    numerator: int,
    denominator: int,
    *,
    prev_input=None,
    expiration=None,
) -> CardanoSwapsSwapDatum:
    pair = _pair_beacon(offer_id, offer_name, ask_id, ask_name)
    if prev_input is None:
        prev = PlutusNone()
    else:
        prev = prev_input
    if expiration is None:
        exp = PlutusNone()
    else:
        exp = expiration
    return CardanoSwapsSwapDatum(
        beacon_id=bytes.fromhex(BEACON_POLICY_ID),
        pair_beacon=pair,
        offer_id=offer_id,
        offer_name=offer_name,
        offer_beacon=_offer_beacon(offer_id, offer_name),
        ask_id=ask_id,
        ask_name=ask_name,
        ask_beacon=_ask_beacon(ask_id, ask_name),
        swap_price=CardanoSwapsRational(numerator=numerator, denominator=denominator),
        prev_input=prev,
        expiration=exp,
    )


def _values_from_datum(
    datum: CardanoSwapsSwapDatum,
    assets: Assets,
) -> dict:
    """A synthetic UTxO values dict in the dbsync record shape."""
    return {
        "tx_hash": TX_HASH,
        "tx_index": 0,
        "datum_cbor": datum.to_cbor_hex(),
        "datum_hash": datum.hash().to_primitive().hex(),
        "assets": assets,
        "block_time": 0,
        "block_index": 0,
        "plutus_v2": True,
    }


# --- datum round-trip ------------------------------------------------------


def test_datum_roundtrip_none_optionals() -> None:
    datum = _make_datum(b"", b"", bytes.fromhex(TOKEN_A_POLICY), b"AAA", 3, 2)
    rt = CardanoSwapsSwapDatum.from_cbor(datum.to_cbor_hex())
    assert rt == datum
    assert isinstance(rt.prev_input, PlutusNone)
    assert isinstance(rt.expiration, PlutusNone)
    assert rt.swap_price.numerator == 3
    assert rt.swap_price.denominator == 2


def test_datum_roundtrip_some_optionals() -> None:
    prev = CardanoSwapsSomeOutRef(
        value=CardanoSwapsOutputReference(
            transaction_id=CardanoSwapsTxId(tx_hash=bytes.fromhex(TX_HASH)),
            output_index=2,
        ),
    )
    exp = CardanoSwapsSomeInt(value=1_700_000_000_000)
    datum = _make_datum(
        bytes.fromhex(TOKEN_A_POLICY),
        b"AAA",
        b"",
        b"",
        5,
        7,
        prev_input=prev,
        expiration=exp,
    )
    rt = CardanoSwapsSwapDatum.from_cbor(datum.to_cbor_hex())
    assert rt == datum
    assert isinstance(rt.prev_input, CardanoSwapsSomeOutRef)
    assert rt.prev_input.value.output_index == 2
    assert rt.prev_input.value.transaction_id.tx_hash == bytes.fromhex(TX_HASH)
    assert isinstance(rt.expiration, CardanoSwapsSomeInt)
    assert rt.expiration.value == 1_700_000_000_000


def test_datum_eleven_fields_constr_zero() -> None:
    datum = _make_datum(b"", b"", bytes.fromhex(TOKEN_A_POLICY), b"AAA", 1, 1)
    assert datum.CONSTR_ID == 0
    # 11 dataclass fields in exact order.
    field_names = list(datum.__dataclass_fields__.keys())
    assert field_names == [
        "beacon_id",
        "pair_beacon",
        "offer_id",
        "offer_name",
        "offer_beacon",
        "ask_id",
        "ask_name",
        "ask_beacon",
        "swap_price",
        "prev_input",
        "expiration",
    ]


# --- order-state parsing ---------------------------------------------------


def test_parse_ada_offer_token_ask() -> None:
    """Offer ADA, ask token AAA. price = ask-per-offer = 2/1 (2 AAA per ADA)."""
    offer_qty = 10_000_000  # lovelace held in the UTxO
    datum = _make_datum(
        b"",  # offer = ADA
        b"",
        bytes.fromhex(TOKEN_A_POLICY),  # ask = AAA
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
    )
    # beacons live under the beacon policy; include all three + the ADA offer.
    assets = Assets(
        root={
            "lovelace": offer_qty,
            BEACON_POLICY_ID + datum.pair_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.offer_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.ask_beacon.hex(): 1,
        },
    )
    state = CardanoSwapsOrderState.model_validate(_values_from_datum(datum, assets))

    # taker orientation: in=ask (AAA), out=offer (ADA)
    assert state.in_unit == TOKEN_A_UNIT
    assert state.out_unit == "lovelace"
    assert state.price == (2, 1)
    assert state.available.unit() == "lovelace"
    assert state.available.quantity() == offer_qty

    # beacons stripped from the tradable balance
    assert all(not u.startswith(BEACON_POLICY_ID) for u in state.assets.keys())
    assert state.pool_id == datum.pair_beacon.hex()


def test_parse_token_offer_ada_ask() -> None:
    """Offer token AAA, ask ADA. price = ask-per-offer = 3/2."""
    offer_qty = 5_000
    datum = _make_datum(
        bytes.fromhex(TOKEN_A_POLICY),  # offer = AAA
        bytes.fromhex(TOKEN_A_NAME),
        b"",  # ask = ADA
        b"",
        3,
        2,
    )
    assets = Assets(
        root={
            "lovelace": 2_000_000,  # min-ADA deposit; ADA is the ask side here
            TOKEN_A_UNIT: offer_qty,
            BEACON_POLICY_ID + datum.pair_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.offer_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.ask_beacon.hex(): 1,
        },
    )
    state = CardanoSwapsOrderState.model_validate(_values_from_datum(datum, assets))

    assert state.in_unit == "lovelace"  # ask = ADA
    assert state.out_unit == TOKEN_A_UNIT  # offer = AAA
    assert state.price == (3, 2)
    assert state.available.unit() == TOKEN_A_UNIT
    assert state.available.quantity() == offer_qty
    assert all(not u.startswith(BEACON_POLICY_ID) for u in state.assets.keys())


def test_parse_token_offer_token_ask() -> None:
    """Offer token AAA, ask token BBB. price = ask-per-offer = 7/5."""
    offer_qty = 1_234
    datum = _make_datum(
        bytes.fromhex(TOKEN_A_POLICY),  # offer = AAA
        bytes.fromhex(TOKEN_A_NAME),
        bytes.fromhex(TOKEN_B_POLICY),  # ask = BBB
        bytes.fromhex(TOKEN_B_NAME),
        7,
        5,
    )
    assets = Assets(
        root={
            "lovelace": 2_000_000,  # min-ADA, neither offer nor ask
            TOKEN_A_UNIT: offer_qty,
            BEACON_POLICY_ID + datum.pair_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.offer_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.ask_beacon.hex(): 1,
        },
    )
    state = CardanoSwapsOrderState.model_validate(_values_from_datum(datum, assets))

    assert state.in_unit == TOKEN_B_UNIT  # ask = BBB
    assert state.out_unit == TOKEN_A_UNIT  # offer = AAA
    assert state.price == (7, 5)
    assert state.available.unit() == TOKEN_A_UNIT
    assert state.available.quantity() == offer_qty
    assert all(not u.startswith(BEACON_POLICY_ID) for u in state.assets.keys())


# --- pricing sanity --------------------------------------------------------


def test_get_amount_out_at_price() -> None:
    """Giving the ask asset yields the offer asset at price den/num.

    offer=ADA, ask=AAA, price=2/1 (2 AAA asked per 1 ADA offered).
    Giving 4 AAA should yield 2 ADA (4 * 1/2 = 2).
    """
    offer_qty = 10_000_000
    datum = _make_datum(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
    )
    assets = Assets(
        root={
            "lovelace": offer_qty,
            BEACON_POLICY_ID + datum.pair_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.offer_beacon.hex(): 1,
            BEACON_POLICY_ID + datum.ask_beacon.hex(): 1,
        },
    )
    state = CardanoSwapsOrderState.model_validate(_values_from_datum(datum, assets))

    out, _ = state.get_amount_out(Assets(root={TOKEN_A_UNIT: 4}))
    assert out.unit() == "lovelace"
    assert out.quantity() == 2  # 4 * den/num = 4 * 1/2


def test_order_datum_helpers() -> None:
    datum = _make_datum(
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        bytes.fromhex(TOKEN_B_POLICY),
        bytes.fromhex(TOKEN_B_NAME),
        1,
        1,
    )
    assert datum.offer_unit() == TOKEN_A_UNIT
    assert datum.ask_unit() == TOKEN_B_UNIT
    assert datum.order_type() == OrderType.swap
    assert datum.address_source() is None
    assert datum.requested_amount().unit() == TOKEN_B_UNIT
    pair = datum.pool_pair()
    assert TOKEN_A_UNIT in pair.keys()
    assert TOKEN_B_UNIT in pair.keys()


# --- expiration liveness ---------------------------------------------------


def _utxo_assets(datum: CardanoSwapsSwapDatum, base: dict) -> Assets:
    """Build UTxO assets = ``base`` plus the three beacon tokens."""
    root = dict(base)
    for beacon in (datum.pair_beacon, datum.offer_beacon, datum.ask_beacon):
        root[BEACON_POLICY_ID + beacon.hex()] = 1
    return Assets(root=root)


def test_expiration_controls_inactive() -> None:
    """Expired swap parses inactive; future / no expiry parse active."""
    base = {"lovelace": 10_000_000}
    ask = (bytes.fromhex(TOKEN_A_POLICY), bytes.fromhex(TOKEN_A_NAME))

    expired = _make_datum(b"", b"", *ask, 2, 1, expiration=CardanoSwapsSomeInt(value=1))
    state = CardanoSwapsOrderState.model_validate(
        _values_from_datum(expired, _utxo_assets(expired, base)),
    )
    assert state.inactive is True

    future = _make_datum(
        b"",
        b"",
        *ask,
        2,
        1,
        expiration=CardanoSwapsSomeInt(value=9_999_999_999_999),
    )
    state = CardanoSwapsOrderState.model_validate(
        _values_from_datum(future, _utxo_assets(future, base)),
    )
    assert state.inactive is False

    never = _make_datum(b"", b"", *ask, 2, 1)
    state = CardanoSwapsOrderState.model_validate(
        _values_from_datum(never, _utxo_assets(never, base)),
    )
    assert state.inactive is False


# --- raw wire structure (independent of this module's classes) -------------


def test_datum_wire_structure() -> None:
    """Assert the raw CBOR shape so a CONSTR_ID/field-order regression is caught.

    A self-referential round-trip would still pass with a wrong constructor tag;
    decoding the bytes directly does not.
    """
    prev = CardanoSwapsSomeOutRef(
        value=CardanoSwapsOutputReference(
            transaction_id=CardanoSwapsTxId(tx_hash=bytes.fromhex(TX_HASH)),
            output_index=1,
        ),
    )
    datum = _make_datum(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        3,
        2,
        prev_input=prev,
        expiration=CardanoSwapsSomeInt(value=42),
    )
    decoded = cbor2.loads(bytes.fromhex(datum.to_cbor_hex()))

    # Constr 0 == CBOR tag 121, with the 11 fields in order.
    assert decoded.tag == 121
    fields = decoded.value
    assert len(fields) == 11
    # swap_price (index 8) == Constr 0 [num, den]
    assert fields[8].tag == 121
    assert list(fields[8].value) == [3, 2]
    # Some prev_input / expiration == Constr 0; expiration carries [42]
    assert fields[9].tag == 121
    assert fields[10].tag == 121
    assert list(fields[10].value) == [42]

    # None optionals encode as Constr 1 (tag 122) with no fields.
    none_datum = _make_datum(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        1,
        1,
    )
    none_fields = cbor2.loads(bytes.fromhex(none_datum.to_cbor_hex())).value
    assert none_fields[9].tag == 122
    assert none_fields[10].tag == 122


# --- transaction-builder structural tests ----------------------------------


class _OfflineContext(ChainContext):
    """A network-free chain context: enough for ``min_lovelace`` + builder mutation.

    The structural builder tests only assemble script inputs, mints and outputs;
    they never balance, sign, or evaluate, so the protocol parameters just need
    to satisfy ``min_lovelace_post_alonzo`` (``coins_per_utxo_byte``).
    """

    @property
    def protocol_param(self) -> ProtocolParameters:
        return ProtocolParameters(
            min_fee_constant=155381,
            min_fee_coefficient=44,
            max_block_size=98304,
            max_tx_size=16384,
            max_block_header_size=1100,
            key_deposit=2000000,
            pool_deposit=500000000,
            pool_influence=0.3,
            monetary_expansion=0.003,
            treasury_expansion=0.2,
            decentralization_param=0,
            extra_entropy="",
            protocol_major_version=8,
            protocol_minor_version=0,
            min_utxo=1000000,
            min_pool_cost=340000000,
            price_mem=0.0577,
            price_step=0.0000721,
            max_tx_ex_mem=14000000,
            max_tx_ex_steps=10000000000,
            max_block_ex_mem=62000000,
            max_block_ex_steps=20000000000,
            max_val_size=5000,
            collateral_percent=150,
            max_collateral_inputs=3,
            coins_per_utxo_word=34482,
            coins_per_utxo_byte=4310,
            cost_models={},
        )

    @property
    def genesis_param(self):
        return None

    @property
    def network(self) -> Network:
        return Network.MAINNET

    @property
    def epoch(self) -> int:
        return 0

    @property
    def last_block_slot(self) -> int:
        return 0

    def utxos(self, address):
        return []

    def submit_tx_cbor(self, cbor):
        return ""

    def evaluate_tx_cbor(self, cbor):
        return {}


OWNER = Address(
    payment_part=VerificationKeyHash(bytes.fromhex("11" * 28)),
    staking_part=VerificationKeyHash(bytes.fromhex("22" * 28)),
    network=Network.MAINNET,
)


@pytest.fixture
def tx_builder() -> TransactionBuilder:
    return TransactionBuilder(_OfflineContext())


def _mint_dict(tx_builder: TransactionBuilder) -> dict[str, int]:
    """Flatten the builder's mint MultiAsset into a ``unit -> quantity`` dict."""
    if tx_builder.mint is None:
        return {}
    return {
        bytes(policy).hex() + bytes(name).hex(): qty
        for policy, names in tx_builder.mint.data.items()
        for name, qty in names.items()
    }


def _output_units(txo) -> set[str]:
    """The set of ``policy+name`` units (non-ADA) carried by an output."""
    return {
        bytes(policy).hex() + bytes(name).hex()
        for policy, names in txo.amount.multi_asset.data.items()
        for name in names
    }


def _redeemers(tx_builder: TransactionBuilder) -> list:
    """The script-input redeemers attached to the builder."""
    return [r for r in tx_builder._inputs_to_redeemers.values() if r is not None]


def _resting_state(
    offer_id: bytes,
    offer_name: bytes,
    ask_id: bytes,
    ask_name: bytes,
    numerator: int,
    denominator: int,
    offer_qty: int,
    *,
    extra: dict | None = None,
) -> CardanoSwapsOrderState:
    """Parse a resting swap order state from a synthetic datum + UTxO."""
    datum = _make_datum(
        offer_id,
        offer_name,
        ask_id,
        ask_name,
        numerator,
        denominator,
    )
    base = dict(extra or {})
    offer_unit = "lovelace" if offer_id == b"" else (offer_id.hex() + offer_name.hex())
    base[offer_unit] = base.get(offer_unit, 0) + offer_qty
    assets = _utxo_assets(datum, base)
    return CardanoSwapsOrderState.model_validate(_values_from_datum(datum, assets))


# --- script hashing --------------------------------------------------------


def test_compiled_scripts_hash_to_constants() -> None:
    """The inlined compiled scripts hash to the published policy/validator ids."""
    assert (
        bytes(plutus_script_hash(CardanoSwapsOrderState._swap_script())).hex()
        == SWAP_VALIDATOR_HASH
    )
    assert (
        bytes(plutus_script_hash(CardanoSwapsOrderState._beacon_script())).hex()
        == BEACON_POLICY_ID
    )


# --- CREATE ----------------------------------------------------------------


def test_build_create_mints_three_beacons_and_datum(tx_builder) -> None:
    """CREATE mints +1 of each beacon and the output datum matches offer/ask/price."""
    offer = Assets(root={"lovelace": 10_000_000})
    ask = Assets(root={TOKEN_A_UNIT: 0})

    txo, datum = CardanoSwapsOrderState.build_create(
        owner_address=OWNER,
        offer=offer,
        ask=ask,
        price=(2, 1),
        tx_builder=tx_builder,
    )

    # Exactly +1 of each of the three beacons under the beacon policy.
    mint = _mint_dict(tx_builder)
    assert len(mint) == 3
    assert all(qty == 1 for qty in mint.values())
    assert all(unit.startswith(BEACON_POLICY_ID) for unit in mint)
    assert set(mint) == {
        BEACON_POLICY_ID + datum.pair_beacon.hex(),
        BEACON_POLICY_ID + datum.offer_beacon.hex(),
        BEACON_POLICY_ID + datum.ask_beacon.hex(),
    }

    # Beacon redeemer is CreateOrCloseSwaps (Constr1).
    mint_redeemers = list(tx_builder._minting_script_to_redeemers)
    assert len(mint_redeemers) == 1
    assert isinstance(mint_redeemers[0][1].data, CreateOrCloseSwaps)

    # Datum beacon fields + price are correct (offer ADA, ask AAA).
    assert datum.beacon_id == bytes.fromhex(BEACON_POLICY_ID)
    assert datum.pair_beacon == pair_beacon_name(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
    )
    assert datum.offer_beacon == offer_beacon_name(b"", b"")
    assert datum.ask_beacon == ask_beacon_name(
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
    )
    assert datum.swap_price.numerator == 2
    assert datum.swap_price.denominator == 1
    assert isinstance(datum.prev_input, PlutusNone)
    assert isinstance(datum.expiration, PlutusNone)

    # Output is at the owner's beacon-tagged swap address with the inline datum.
    assert bytes(txo.address.payment_part).hex() == SWAP_VALIDATOR_HASH
    assert txo.address.staking_part == OWNER.staking_part
    assert txo.datum == datum
    assert _output_units(txo) == set(mint)  # the 3 beacons (offer is ADA)
    assert txo in tx_builder.outputs


def test_build_create_ada_offer_funds_drawable_carrier(tx_builder) -> None:
    """An ADA-offer CREATE funds offer + a carrier sized to the FINAL full-fill
    state (3 beacons + fully-accumulated ask + datum), so the ENTIRE offered ADA
    is drawable — no phantom, un-fillable min-ADA tail.

    The validator derives ``offer_taken = lovelace_in - lovelace_out``, so the
    resting UTxO can never be drawn below its (ask-laden) min-ADA floor; funding
    that floor as a separate carrier on top of the offer makes the full offer
    takeable.
    """
    offer_qty = 10_000_000
    num, den = 2, 1
    txo, datum = CardanoSwapsOrderState.build_create(
        owner_address=OWNER,
        offer=Assets(root={"lovelace": offer_qty}),
        ask=Assets(root={TOKEN_A_UNIT: 0}),
        price=(num, den),
        tx_builder=tx_builder,
    )

    # Independently size the floor the continuation must hold at a FULL fill:
    # 3 beacons + the fully-accumulated ask (offer × price) + the inline datum.
    full_ask = -(-offer_qty * num // den)
    final_assets = CardanoSwapsOrderState._beacon_mint_assets(datum, 1) + Assets(
        root={TOKEN_A_UNIT: full_ask},
    )
    final_txo = TransactionOutput(
        address=txo.address,
        amount=asset_to_value(final_assets),
        datum=datum,
    )
    carrier = min_lovelace(tx_builder.context, output=final_txo)

    # Resting UTxO holds exactly offer + carrier; the whole offer is drawable.
    assert txo.amount.coin == offer_qty + carrier
    assert txo.amount.coin - carrier == offer_qty


def test_build_create_expiration_set(tx_builder) -> None:
    """A CREATE with an expiration carries Some(expiration) inline."""
    _, datum = CardanoSwapsOrderState.build_create(
        owner_address=OWNER,
        offer=Assets(root={TOKEN_A_UNIT: 5_000}),
        ask=Assets(root={"lovelace": 0}),
        price=(3, 2),
        tx_builder=tx_builder,
        expiration=1_700_000_040_000,
    )
    assert isinstance(datum.expiration, CardanoSwapsSomeInt)
    assert datum.expiration.value == 1_700_000_040_000
    # Token offer => the output carries the offer token + 3 beacons.
    assert datum.offer_beacon == offer_beacon_name(
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
    )


# --- FILL ------------------------------------------------------------------


def test_build_fill_partial(tx_builder) -> None:
    """A partial FILL: Swap redeemer, maker paid, continuation with new prev_input."""
    # Offer ADA (10 ADA), ask AAA, price 2/1 (2 AAA per 1 ADA).
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
        10_000_000,
    )
    datum = state.order_datum

    out_assets = Assets(root={"lovelace": 4_000_000})  # taker takes 4 ADA of offer
    in_assets = Assets(root={TOKEN_A_UNIT: 8_000_000})  # taker pays AAA

    cont_txo, cont_datum = state.swap_utxo(
        address_source=OWNER,
        in_assets=in_assets,
        out_assets=out_assets,
        tx_builder=tx_builder,
        owner_address=OWNER,
    )

    # Spend uses the field-less Swap redeemer (Constr2).
    reds = _redeemers(tx_builder)
    assert any(isinstance(r.data, Swap) for r in reds)

    # No separate maker payment — the ask accumulates in the continuation.
    assert not [
        o
        for o in tx_builder.outputs
        if o is not cont_txo and o.address == OWNER and TOKEN_A_UNIT in _output_units(o)
    ]
    # Continuation holds the ask paid in: ask_given = ceil(4_000_000 * 2 / 1) =
    # 8_000_000, and the ADA offer is decremented by the 4 ADA taken (10 -> 6).
    cont_ask = next(
        qty
        for policy, names in cont_txo.amount.multi_asset.data.items()
        for name, qty in names.items()
        if bytes(policy).hex() + bytes(name).hex() == TOKEN_A_UNIT
    )
    assert cont_ask == 8_000_000
    assert cont_txo.amount.coin == 6_000_000

    # Continuation output: same swap address, beacons preserved, decremented offer,
    # prev_input -> consumed TxOutRef, all other datum fields identical.
    assert bytes(cont_txo.address.payment_part).hex() == SWAP_VALIDATOR_HASH
    assert cont_txo.address.staking_part == OWNER.staking_part
    cont_units = _output_units(cont_txo)
    assert BEACON_POLICY_ID + datum.pair_beacon.hex() in cont_units
    assert BEACON_POLICY_ID + datum.offer_beacon.hex() in cont_units
    assert BEACON_POLICY_ID + datum.ask_beacon.hex() in cont_units

    assert isinstance(cont_datum.prev_input, CardanoSwapsSomeOutRef)
    assert cont_datum.prev_input.value.transaction_id.tx_hash == bytes.fromhex(TX_HASH)
    assert cont_datum.prev_input.value.output_index == 0
    # Everything else is byte-identical to the input datum.
    assert cont_datum.pair_beacon == datum.pair_beacon
    assert cont_datum.offer_beacon == datum.offer_beacon
    assert cont_datum.ask_beacon == datum.ask_beacon
    assert cont_datum.swap_price == datum.swap_price
    assert cont_datum.offer_id == datum.offer_id
    assert cont_datum.ask_id == datum.ask_id
    assert cont_datum.expiration == datum.expiration

    # No mint/burn on a fill.
    assert _mint_dict(tx_builder) == {}


def test_build_fill_token_to_token(tx_builder) -> None:
    """A token->token FILL still pays the maker and preserves beacons."""
    # Offer AAA, ask BBB, price 7/5 (7 BBB per 5 AAA).
    state = _resting_state(
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        bytes.fromhex(TOKEN_B_POLICY),
        bytes.fromhex(TOKEN_B_NAME),
        7,
        5,
        10_000,
        extra={"lovelace": 2_000_000},
    )

    out_assets = Assets(root={TOKEN_A_UNIT: 5_000})  # taker takes 5000 AAA
    in_assets = Assets(root={TOKEN_B_UNIT: 7_000})  # pays >= ceil(5000*7/5)=7000 BBB

    cont_txo, cont_datum = state.swap_utxo(
        address_source=OWNER,
        in_assets=in_assets,
        out_assets=out_assets,
        tx_builder=tx_builder,
        owner_address=OWNER,
    )

    assert any(isinstance(r.data, Swap) for r in _redeemers(tx_builder))
    # No separate maker payment — the ask (BBB) accumulates in the continuation.
    assert not [
        o
        for o in tx_builder.outputs
        if o is not cont_txo and o.address == OWNER and TOKEN_B_UNIT in _output_units(o)
    ]
    cont_units = {
        bytes(policy).hex() + bytes(name).hex(): qty
        for policy, names in cont_txo.amount.multi_asset.data.items()
        for name, qty in names.items()
    }
    # Continuation keeps the remaining offer (10000 - 5000 = 5000 AAA) and holds
    # the ask paid in (>= ceil(5000 * 7 / 5) = 7000 BBB).
    assert cont_units[TOKEN_A_UNIT] == 5_000
    assert cont_units[TOKEN_B_UNIT] == 7_000
    assert isinstance(cont_datum.prev_input, CardanoSwapsSomeOutRef)


# --- CLOSE -----------------------------------------------------------------


def test_build_close_burns_three_beacons(tx_builder) -> None:
    """CLOSE spends with SpendWithMint (Constr0) and burns -1 of each beacon."""
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
        10_000_000,
    )
    datum = state.order_datum

    spent_datum = state.build_close(tx_builder=tx_builder, owner_address=OWNER)

    # Burns exactly -1 of each of the three beacons.
    mint = _mint_dict(tx_builder)
    assert len(mint) == 3
    assert all(qty == -1 for qty in mint.values())
    assert set(mint) == {
        BEACON_POLICY_ID + datum.pair_beacon.hex(),
        BEACON_POLICY_ID + datum.offer_beacon.hex(),
        BEACON_POLICY_ID + datum.ask_beacon.hex(),
    }

    # Spend uses SpendWithMint (Constr0); beacon burn uses CreateOrCloseSwaps.
    assert any(isinstance(r.data, SpendWithMint) for r in _redeemers(tx_builder))
    mint_redeemers = list(tx_builder._minting_script_to_redeemers)
    assert len(mint_redeemers) == 1
    assert isinstance(mint_redeemers[0][1].data, CreateOrCloseSwaps)

    # The spent datum is the resting datum.
    assert spent_datum == datum


def test_redeemer_constructor_indices() -> None:
    """The redeemer constructor indices match the validator declaration order."""
    from charli3_dendrite.dexs.ob.cardanoswaps import RegisterBeaconScript
    from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithStake
    from charli3_dendrite.dexs.ob.cardanoswaps import UpdateSwaps

    assert SpendWithMint.CONSTR_ID == 0
    assert SpendWithStake.CONSTR_ID == 1
    assert Swap.CONSTR_ID == 2
    assert RegisterBeaconScript.CONSTR_ID == 0
    assert CreateOrCloseSwaps.CONSTR_ID == 1
    assert UpdateSwaps.CONSTR_ID == 2


def test_beacon_minting_script_is_dapp_hash_applied_blueprint() -> None:
    """The baked beacon minting script is the blueprint script with ``dapp_hash`` applied.

    The beacon minting policy is a parameterized validator; its deployed
    minting-policy id (``BEACON_POLICY_ID``) is the hash of the
    ``one_way_swap.beacon_script`` blueprint code with ``dapp_hash`` (= the swap
    validator hash) applied. Re-derive it in pure Python via ``uplc``,
    independently of aiken, and assert the inlined script + policy id match — so
    the baked constants can never silently drift from the on-chain protocol.

    ``uplc`` is a dev/CI dependency; this guard intentionally has no
    ``importorskip`` so a missing ``uplc`` fails the suite rather than skipping.
    """
    unapplied = (
        (Path(__file__).parent / "data" / "cardanoswaps_one_way_beacon_unapplied.hex")
        .read_text()
        .strip()
    )

    applied = apply_params_to_script(
        bytes.fromhex(unapplied), bytes.fromhex(SWAP_VALIDATOR_HASH)
    )

    assert applied.hex() == BEACON_POLICY_SCRIPT_HEX
    assert str(plutus_script_hash(PlutusV2Script(applied))) == BEACON_POLICY_ID


# --- reference scripts -----------------------------------------------------


def _ref_utxo(script: PlutusV2Script, *, index: int = 0) -> UTxO:
    """A synthetic reference-script UTxO carrying ``script`` in its output."""
    return UTxO(
        TransactionInput(TransactionId(bytes.fromhex("ab" * 32)), index),
        TransactionOutput(address=OWNER, amount=Value(coin=5_000_000), script=script),
    )


def test_build_create_references_beacon_when_supplied(tx_builder) -> None:
    """CREATE references the beacon policy via a reference input when a ref UTxO is given."""
    beacon_ref = _ref_utxo(CardanoSwapsOrderState._beacon_script())
    CardanoSwapsOrderState.build_create(
        owner_address=OWNER,
        offer=Assets(root={"lovelace": 10_000_000}),
        ask=Assets(root={TOKEN_A_UNIT: 0}),
        price=(2, 1),
        tx_builder=tx_builder,
        beacon_ref_utxo=beacon_ref,
    )
    # The policy is referenced (reference input), not inlined; mint is unchanged.
    assert beacon_ref in tx_builder.reference_inputs
    mint = _mint_dict(tx_builder)
    assert len(mint) == 3
    assert all(qty == 1 for qty in mint.values())


def test_build_fill_references_swap_when_supplied(tx_builder) -> None:
    """FILL references the swap validator via a reference input when a ref UTxO is given."""
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
        10_000_000,
    )
    swap_ref = _ref_utxo(CardanoSwapsOrderState._swap_script())
    state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={TOKEN_A_UNIT: 8_000_000}),
        out_assets=Assets(root={"lovelace": 4_000_000}),
        tx_builder=tx_builder,
        owner_address=OWNER,
        swap_ref_utxo=swap_ref,
    )
    assert swap_ref in tx_builder.reference_inputs
    assert any(isinstance(r.data, Swap) for r in _redeemers(tx_builder))


def test_build_close_references_both_when_supplied(tx_builder) -> None:
    """CLOSE references both the swap validator and the beacon policy when refs are given."""
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
        10_000_000,
    )
    swap_ref = _ref_utxo(CardanoSwapsOrderState._swap_script(), index=0)
    beacon_ref = _ref_utxo(CardanoSwapsOrderState._beacon_script(), index=1)
    state.build_close(
        tx_builder=tx_builder,
        owner_address=OWNER,
        swap_ref_utxo=swap_ref,
        beacon_ref_utxo=beacon_ref,
    )
    assert swap_ref in tx_builder.reference_inputs
    assert beacon_ref in tx_builder.reference_inputs
    mint = _mint_dict(tx_builder)
    assert len(mint) == 3
    assert all(qty == -1 for qty in mint.values())
    assert any(isinstance(r.data, SpendWithMint) for r in _redeemers(tx_builder))


def test_inline_is_default_no_reference_inputs() -> None:
    """With no reference UTxO supplied, every builder inlines: no reference inputs."""
    tb = TransactionBuilder(_OfflineContext())
    CardanoSwapsOrderState.build_create(
        owner_address=OWNER,
        offer=Assets(root={"lovelace": 10_000_000}),
        ask=Assets(root={TOKEN_A_UNIT: 0}),
        price=(2, 1),
        tx_builder=tb,
    )
    assert tb.reference_inputs == set()

    state = _resting_state(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
        10_000_000,
    )
    tb = TransactionBuilder(_OfflineContext())
    state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={TOKEN_A_UNIT: 8_000_000}),
        out_assets=Assets(root={"lovelace": 4_000_000}),
        tx_builder=tb,
        owner_address=OWNER,
    )
    assert tb.reference_inputs == set()

    tb = TransactionBuilder(_OfflineContext())
    state.build_close(tx_builder=tb, owner_address=OWNER)
    assert tb.reference_inputs == set()


@pytest.mark.parametrize(
    ("method_name", "wrong_script"),
    [
        ("_swap_script_arg", CardanoSwapsOrderState._beacon_script()),
        ("_beacon_script_arg", CardanoSwapsOrderState._swap_script()),
    ],
)
def test_ref_script_hash_guard_rejects_mismatch(method_name, wrong_script) -> None:
    """A reference UTxO whose script hashes to the wrong policy/validator is rejected."""
    wrong_ref = _ref_utxo(wrong_script)
    with pytest.raises(ValueError, match="reference UTxO script hash"):
        getattr(CardanoSwapsOrderState, method_name)(wrong_ref)


@pytest.mark.parametrize("method_name", ["_swap_script_arg", "_beacon_script_arg"])
def test_ref_script_guard_rejects_missing_script(method_name) -> None:
    """A reference UTxO with no output script is rejected (both script args)."""
    no_script = UTxO(
        TransactionInput(TransactionId(bytes.fromhex("ab" * 32)), 0),
        TransactionOutput(address=OWNER, amount=Value(coin=5_000_000)),
    )
    with pytest.raises(ValueError, match="no output script"):
        getattr(CardanoSwapsOrderState, method_name)(no_script)


# --- CardanoSwapsOrderBook (aggregate order book) --------------------------
#
# The aggregate over many resting one-way swaps: get_book buckets orders into a
# buy/sell book, the inherited base level-walk prices across them (fee == 0), and
# swap_utxo folds a fill order-by-order — threading EACH maker's owner (the resting
# UTxO's staking credential, carried on ``address``) so the continuation lands at
# the right beacon-tagged swap address, not the taker's.

from decimal import Decimal

from pycardano import ScriptHash

from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderBook

_PAIR = Assets(root={"lovelace": 0, TOKEN_A_UNIT: 0})

_MAKER = Address(
    payment_part=VerificationKeyHash(bytes.fromhex("55" * 28)),
    staking_part=VerificationKeyHash(bytes.fromhex("66" * 28)),
    network=Network.MAINNET,
)
_TAKER = Address(
    payment_part=VerificationKeyHash(bytes.fromhex("77" * 28)),
    staking_part=VerificationKeyHash(bytes.fromhex("88" * 28)),
    network=Network.MAINNET,
)


def _sell_order(num: int, den: int, offer_qty: int) -> CardanoSwapsOrderState:
    """A token-offer order (offer AAA, ask ADA) -> the SELL side (a -> b is ADA-in)."""
    return _resting_state(
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        b"",
        b"",
        num,
        den,
        offer_qty,
    )


def _ada_offer_order(num: int, den: int, offer_qty: int) -> CardanoSwapsOrderState:
    """An ADA-offer order (offer ADA, ask AAA) -> the BUY side."""
    return _resting_state(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        num,
        den,
        offer_qty,
    )


def _resting_beacon_addr(owner: Address) -> str:
    """The on-chain resting address: swap-validator payment part + owner staking."""
    return str(
        Address(
            payment_part=ScriptHash(bytes.fromhex(SWAP_VALIDATOR_HASH)),
            staking_part=owner.staking_part,
            network=Network.MAINNET,
        )
    )


def test_orderbook_get_book_orientation_and_levels() -> None:
    sell = _sell_order(2, 1, 100)  # token offer -> sell book, level price 2, qty 100
    buy = _ada_offer_order(3, 1, 10_000_000)  # ADA offer -> buy book
    book = CardanoSwapsOrderBook.get_book(_PAIR, orders=[sell, buy])

    assert book.dex() == "CardanoSwaps"
    assert len(book.sell_book_full) == 1
    assert len(book.buy_book_full) == 1
    assert book.sell_book_full[0].price == 2.0
    assert book.sell_book_full[0].quantity == 100
    assert book.buy_book_full[0].price == 3.0


def test_orderbook_get_amount_out_walks_multiple_levels() -> None:
    # Two token-offer (sell) orders at price 2 and 4; the base level-walk fills the
    # cheaper (ask-per-offer) one first. Prices are raw base-unit rationals.
    cheap = _sell_order(2, 1, 100)  # 100 AAA at 2 lovelace/AAA -> 200 lovelace cap
    dear = _sell_order(4, 1, 100)  # 100 AAA at 4 lovelace/AAA -> 400 lovelace cap
    book = CardanoSwapsOrderBook.get_book(_PAIR, orders=[dear, cheap])  # unordered in

    out, _ = book.get_amount_out(Assets(root={"lovelace": 240}))
    assert out.unit() == TOKEN_A_UNIT
    # 200 lovelace fills all 100 AAA of the cheap level; the remaining 40 buys
    # 40 / 4 = 10 AAA of the dear level. Total 110 AAA.
    assert out.quantity() == 110


def test_orderbook_price_one_sided_returns_zero() -> None:
    # A book with only sell orders (no buy side) must not crash on the base
    # price's buy_book[0] deref — the override returns (0, 0).
    book = CardanoSwapsOrderBook.get_book(_PAIR, orders=[_sell_order(2, 1, 100)])
    assert not book.buy_book_full
    assert book.price == (Decimal(0), Decimal(0))


def test_orderbook_swap_utxo_threads_maker_owner_not_taker(tx_builder) -> None:
    # A fill folds through the resting order's OWN owner (from its address), so the
    # continuation lands at the maker's beacon-tagged swap address — NOT the taker's.
    order = _ada_offer_order(2, 1, 10_000_000)  # offer 10 ADA, ask AAA @ 2 AAA/ADA
    order.address = _resting_beacon_addr(_MAKER)
    book = CardanoSwapsOrderBook.get_book(_PAIR, orders=[order])

    cont_txo, cont_datum = book.swap_utxo(
        address_source=_TAKER,  # the filler — must NOT become the continuation owner
        in_assets=Assets(root={TOKEN_A_UNIT: 4_000_000}),
        out_assets=Assets(root={"lovelace": 2_000_000}),
        tx_builder=tx_builder,
    )

    assert cont_txo is not None
    assert bytes(cont_txo.address.payment_part).hex() == SWAP_VALIDATOR_HASH
    assert cont_txo.address.staking_part == _MAKER.staking_part
    assert cont_txo.address.staking_part != _TAKER.staking_part


def test_orderbook_swap_utxo_requires_address(tx_builder) -> None:
    # An order missing its address (owner unknown) must fail loudly, not silently
    # build a fill owned by the taker.
    order = _ada_offer_order(2, 1, 10_000_000)
    assert order.address is None
    book = CardanoSwapsOrderBook.get_book(_PAIR, orders=[order])

    with pytest.raises(ValueError, match="address"):
        book.swap_utxo(
            address_source=_TAKER,
            in_assets=Assets(root={TOKEN_A_UNIT: 4_000_000}),
            out_assets=Assets(root={"lovelace": 2_000_000}),
            tx_builder=tx_builder,
        )


def test_orderstate_retains_address_from_record() -> None:
    # The backend UTxO record carries the resting address; the order state now
    # retains it (it was dropped before), so an aggregate fill can recover the owner.
    datum = _make_datum(
        b"", b"", bytes.fromhex(TOKEN_A_POLICY), bytes.fromhex(TOKEN_A_NAME), 2, 1
    )
    assets = _utxo_assets(datum, {"lovelace": 10_000_000})
    values = _values_from_datum(datum, assets)
    values["address"] = _resting_beacon_addr(_MAKER)
    state = CardanoSwapsOrderState.model_validate(values)
    assert state.address == _resting_beacon_addr(_MAKER)
