"""Official and legacy Cardano-Swaps one-way contract descriptions."""

from __future__ import annotations

import hashlib
import dataclasses
import json
from pathlib import Path

import cbor2
import pytest
from cbor2 import CBORTag
from pycardano import Address
from pycardano import Network
from pycardano import PlutusV2Script
from pycardano import PlutusV3Script
from pycardano import TransactionBuilder
from pycardano import VerificationKeyHash
from pycardano import plutus_script_hash

from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps import BEACON_POLICY_SCRIPT_HEX
from charli3_dendrite.dexs.ob.cardanoswaps import LEGACY_BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps import LEGACY_CONTRACT
from charli3_dendrite.dexs.ob.cardanoswaps import LEGACY_SWAP_VALIDATOR_HASH
from charli3_dendrite.dexs.ob.cardanoswaps import OFFICIAL_CONTRACT
from charli3_dendrite.dexs.ob.cardanoswaps import SWAP_VALIDATOR_HASH
from charli3_dendrite.dexs.ob.cardanoswaps import SWAP_VALIDATOR_SCRIPT_HEX
from charli3_dendrite.dexs.ob.cardanoswaps import _CardanoSwapsOneWayOrderState
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsLegacyOrderState
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderState
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOutputReference
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOutRefV3
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsRational
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRef
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRefV3
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSwapDatum
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsTxId
from charli3_dendrite.dexs.ob.cardanoswaps import ask_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps import contract_for_beacon
from charli3_dendrite.dexs.ob.cardanoswaps import legacy_ask_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps import legacy_offer_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps import legacy_pair_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps import offer_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps import pair_beacon_name
from tests.test_cardanoswaps import OWNER
from tests.test_cardanoswaps import _mint_dict
from tests.test_cardanoswaps import _OfflineContext

USDM_ID = bytes.fromhex("c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad")
USDM_NAME = bytes.fromhex("0014df105553444d")
DATA = Path(__file__).parent / "data"

# Produced by the official contract code itself (``aiken check`` at commit
# e1ab9157, traces of generate_offer_beacon / generate_ask_beacon /
# generate_pair_beacon).
OFFICIAL_VECTORS = [
    (
        "offer",
        (b"", b""),
        "07d5f63e85046b83e1fc4102a7c19c3f1711c51984725e3b6cf195900947cebe",
    ),
    (
        "ask",
        (b"", b""),
        "08bae3e35a3531a500149bd10d9b872621a41b4f6ba086920518220829370d2b",
    ),
    (
        "offer",
        (USDM_ID, USDM_NAME),
        "a01febd063c6919f19ecafd3114e1203139ba7e8d310672e692b75712ddbdb98",
    ),
    (
        "ask",
        (USDM_ID, USDM_NAME),
        "3e785839fd0f65a4689c51fa4f1dfe27066922ecfe66fa5ad8b2c5ea2ec0e055",
    ),
    (
        "pair",
        (b"", b"", USDM_ID, USDM_NAME),
        "c35c0f3115aec28da91f69abd64195d2731a5e600af8e61ca5aab940916652b8",
    ),
    (
        "pair",
        (USDM_ID, USDM_NAME, b"", b""),
        "188c77e064f50480dff4048c467c0d7c9e4c023715da2391c1e8b1152783f42b",
    ),
]
_FNS = {"offer": offer_beacon_name, "ask": ask_beacon_name, "pair": pair_beacon_name}


@pytest.mark.parametrize(("kind", "args", "expected"), OFFICIAL_VECTORS)
def test_official_beacon_names_match_the_contract(kind, args, expected) -> None:
    assert _FNS[kind](*args).hex() == expected


def test_legacy_beacon_names_keep_the_prefix_rule() -> None:
    sha = lambda b: hashlib.sha256(b).digest()  # noqa: E731
    assert legacy_offer_beacon_name(b"", b"") == sha(b"\x01")
    assert legacy_ask_beacon_name(USDM_ID, USDM_NAME) == sha(
        b"\x02" + USDM_ID + USDM_NAME
    )
    assert legacy_pair_beacon_name(b"", b"", USDM_ID, USDM_NAME) == sha(
        b"\x00" + b"" + USDM_ID + USDM_NAME
    )


def test_official_contract_scripts_are_the_v2_0_0_blueprint() -> None:
    blueprint = json.loads((DATA / "cardanoswaps_official_scripts.json").read_text())
    assert SWAP_VALIDATOR_SCRIPT_HEX == blueprint["swap_validator_hex"]
    assert BEACON_POLICY_SCRIPT_HEX == blueprint["beacon_policy_hex"]
    assert OFFICIAL_CONTRACT.script_class is PlutusV3Script
    assert (
        plutus_script_hash(OFFICIAL_CONTRACT.swap_script()).payload.hex()
        == SWAP_VALIDATOR_HASH
    )
    assert (
        plutus_script_hash(OFFICIAL_CONTRACT.beacon_script()).payload.hex()
        == BEACON_POLICY_ID
    )
    assert (
        SWAP_VALIDATOR_HASH
        == "e5a22e4c31db20bce1c8b081f8e4009683990a33157947d75030deb8"
    )
    assert (
        BEACON_POLICY_ID == "274765b4c626c28d18752176b59c0ff63db56b8305c1daa49c9879fe"
    )


def test_legacy_contract_scripts_are_unchanged() -> None:
    assert LEGACY_CONTRACT.script_class is PlutusV2Script
    assert (
        LEGACY_SWAP_VALIDATOR_HASH
        == "1d6cff26bcab91d2061aad0bd259cbb7d76d25ced2eeaed5926a42ad"
    )
    assert (
        LEGACY_BEACON_POLICY_ID
        == "c4d7d117d9ebcde6db28db40837ff2b1401e9eaaa6eecea9e070e209"
    )
    assert (
        plutus_script_hash(LEGACY_CONTRACT.swap_script()).payload.hex()
        == LEGACY_SWAP_VALIDATOR_HASH
    )
    assert (
        plutus_script_hash(LEGACY_CONTRACT.beacon_script()).payload.hex()
        == LEGACY_BEACON_POLICY_ID
    )


def test_contract_capabilities() -> None:
    assert (OFFICIAL_CONTRACT.allows_create, OFFICIAL_CONTRACT.allows_fill) == (
        True,
        True,
    )
    assert OFFICIAL_CONTRACT.create_needs_stake_signature is True
    assert (LEGACY_CONTRACT.allows_create, LEGACY_CONTRACT.allows_fill) == (
        False,
        False,
    )
    assert OFFICIAL_CONTRACT.pair_beacon_name is pair_beacon_name
    assert LEGACY_CONTRACT.pair_beacon_name is legacy_pair_beacon_name


TOKEN = "a" * 56 + "414141"


def _legacy_state(
    offer_qty: int = 10_000_000,
    prev_input: CardanoSwapsSomeOutRef | PlutusNone | None = None,
) -> CardanoSwapsLegacyOrderState:
    """A swap on the pre-release build: ADA offered for TOKEN at 2/1.

    Resting (``prev_input = None``) unless ``prev_input`` is given.
    """
    token_id, token_name = bytes.fromhex("a" * 56), bytes.fromhex("414141")
    datum = CardanoSwapsSwapDatum(
        beacon_id=bytes.fromhex(LEGACY_BEACON_POLICY_ID),
        pair_beacon=legacy_pair_beacon_name(b"", b"", token_id, token_name),
        offer_id=b"",
        offer_name=b"",
        offer_beacon=legacy_offer_beacon_name(b"", b""),
        ask_id=token_id,
        ask_name=token_name,
        ask_beacon=legacy_ask_beacon_name(token_id, token_name),
        swap_price=CardanoSwapsRational(numerator=2, denominator=1),
        prev_input=prev_input if prev_input is not None else PlutusNone(),
        expiration=PlutusNone(),
    )
    assets = Assets(root={"lovelace": offer_qty, **datum.beacon_assets(1).root})
    return CardanoSwapsLegacyOrderState.model_validate(
        {
            "tx_hash": "c" * 64,
            "tx_index": 0,
            "datum_cbor": datum.to_cbor_hex(),
            "datum_hash": datum.hash().to_primitive().hex(),
            "assets": assets,
            "block_time": 0,
            "block_index": 0,
            "plutus_v2": True,
        }
    )


def test_classes_share_the_dex_but_not_the_contract() -> None:
    assert (
        CardanoSwapsOrderState.dex()
        == CardanoSwapsLegacyOrderState.dex()
        == "CardanoSwaps"
    )
    assert CardanoSwapsOrderState.CONTRACT is OFFICIAL_CONTRACT
    assert CardanoSwapsLegacyOrderState.CONTRACT is LEGACY_CONTRACT
    assert CardanoSwapsOrderState.dex_policy() == [BEACON_POLICY_ID]
    assert CardanoSwapsLegacyOrderState.dex_policy() == [LEGACY_BEACON_POLICY_ID]
    assert not issubclass(CardanoSwapsLegacyOrderState, CardanoSwapsOrderState)
    assert not issubclass(CardanoSwapsOrderState, CardanoSwapsLegacyOrderState)


def test_beacon_address_uses_the_class_contract() -> None:
    official = CardanoSwapsOrderState.beacon_address(OWNER)
    legacy = CardanoSwapsLegacyOrderState.beacon_address(OWNER)
    assert official.payment_part.payload.hex() == SWAP_VALIDATOR_HASH
    assert legacy.payment_part.payload.hex() == LEGACY_SWAP_VALIDATOR_HASH
    assert official.staking_part == legacy.staking_part == OWNER.staking_part


def test_legacy_parses_and_strips_its_own_beacons() -> None:
    state = _legacy_state()
    assert state.dex_nft is not None
    assert all(unit.startswith(LEGACY_BEACON_POLICY_ID) for unit in state.dex_nft.root)
    assert state.out_unit == "lovelace"
    assert state.available.quantity() == 10_000_000


def test_legacy_close_burns_legacy_beacons_with_legacy_scripts() -> None:
    state = _legacy_state()
    tb = TransactionBuilder(_OfflineContext())
    state.build_close(tx_builder=tb, owner_address=OWNER)
    mint = _mint_dict(tb)
    assert len(mint) == 3 and all(q == -1 for q in mint.values())
    assert all(unit.startswith(LEGACY_BEACON_POLICY_ID) for unit in mint)
    ((script, _redeemer),) = tb._minting_script_to_redeemers
    assert isinstance(script, PlutusV2Script)
    assert plutus_script_hash(script).payload.hex() == LEGACY_BEACON_POLICY_ID


def test_legacy_refuses_create_and_fill() -> None:
    tb = TransactionBuilder(_OfflineContext())
    with pytest.raises(ValueError, match="read-and-close only"):
        CardanoSwapsLegacyOrderState.build_create(
            owner_address=OWNER,
            offer=Assets(root={"lovelace": 10_000_000}),
            ask=Assets(root={TOKEN: 0}),
            price=(2, 1),
            tx_builder=tb,
        )
    state = _legacy_state()
    with pytest.raises(ValueError, match="read-and-close only"):
        state.swap_utxo(
            address_source=OWNER,
            in_assets=Assets(root={TOKEN: 8_000_000}),
            out_assets=Assets(root={"lovelace": 4_000_000}),
            tx_builder=tb,
            owner_address=OWNER,
        )
    assert tb.mint is None and not tb.outputs and not tb.inputs


def test_the_shared_base_is_not_a_dex() -> None:
    """Only the concrete classes name the dex.

    DEX discovery treats every class whose ``dex()`` answers as a concrete DEX;
    the shared base has no contract to discover or build with.
    """
    with pytest.raises(NotImplementedError):
        _CardanoSwapsOneWayOrderState.dex()


from pycardano import ScriptHash

from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeInt
from charli3_dendrite.utility import posix_ms_to_slot
from tests.test_cardanoswaps import CPUB
from tests.test_cardanoswaps import _make_datum
from tests.test_cardanoswaps import _resting_state


def _create(owner, tb=None):
    tb = tb or TransactionBuilder(_OfflineContext())
    CardanoSwapsOrderState.build_create(
        owner_address=owner,
        offer=Assets(root={"lovelace": 10_000_000}),
        ask=Assets(root={TOKEN: 0}),
        price=(2, 1),
        tx_builder=tb,
    )
    return tb


def test_official_create_requires_the_owner_stake_key() -> None:
    tb = _create(OWNER)
    assert tb.required_signers == [OWNER.staking_part]


def test_official_create_keeps_existing_signers_once() -> None:
    tb = TransactionBuilder(_OfflineContext())
    tb.required_signers = [OWNER.payment_part, OWNER.staking_part]
    _create(OWNER, tb)
    assert tb.required_signers == [OWNER.payment_part, OWNER.staking_part]


@pytest.mark.parametrize(
    "staking",
    [None, ScriptHash(bytes.fromhex("33" * 28))],
    ids=["no-stake", "script-stake"],
)
def test_official_create_refuses_owner_without_a_stake_key(staking) -> None:
    owner = Address(
        payment_part=OWNER.payment_part, staking_part=staking, network=Network.MAINNET
    )
    tb = TransactionBuilder(_OfflineContext())
    with pytest.raises(ValueError, match="staking key"):
        _create(owner, tb)
    assert tb.mint is None and not tb.outputs


def test_official_create_writes_ada_as_empty_and_no_reference_script() -> None:
    tb = _create(OWNER)
    (txo,) = tb.outputs
    datum = txo.datum
    assert (datum.offer_id, datum.offer_name) == (b"", b"")
    assert txo.script is None


EXPIRATION = 1_790_000_040_000  # a 60 000 ms multiple
EXPIRATION_SLOT = posix_ms_to_slot(EXPIRATION)


class _ContextAt(_OfflineContext):
    """An offline context whose chain tip is a fixed slot."""

    def __init__(self, tip: int) -> None:
        super().__init__()
        self._tip = tip

    @property
    def last_block_slot(self) -> int:
        return self._tip


def _fill(
    tip: int,
    expiration: int | None = EXPIRATION,
    ttl: int | None = None,
) -> TransactionBuilder:
    """Fill an ADA-for-TOKEN swap with the chain tip at ``tip``; return the builder."""
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex("a" * 56),
        bytes.fromhex("414141"),
        2,
        1,
        10_000_000,
    )
    if expiration is not None:
        datum = state.order_datum
        datum.expiration = CardanoSwapsSomeInt(value=expiration)
        state.datum_cbor = datum.to_cbor_hex()
    tb = TransactionBuilder(_ContextAt(tip))
    tb.ttl = ttl
    state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={TOKEN: 8_000_000}),
        out_assets=Assets(root={"lovelace": 4_000_000}),
        tx_builder=tb,
        owner_address=OWNER,
    )
    return tb


def test_fill_of_an_expiring_swap_sets_its_deadline() -> None:
    # The tip is ten minutes before the expiration, inside the one-hour fill
    # window, so the expiration itself bounds the fill.
    tb = _fill(tip=EXPIRATION_SLOT - 600)
    assert tb.ttl == EXPIRATION_SLOT


def test_fill_of_a_far_expiring_swap_caps_its_deadline_an_hour_out() -> None:
    # An expiration two days out lies past the node's forecast horizon, so the
    # bound is one hour (3600 slots) past the tip instead.
    tip = EXPIRATION_SLOT - 48 * 3600
    tb = _fill(tip=tip)
    assert tb.ttl == tip + 3600


@pytest.mark.parametrize(
    ("tip", "ttl"),
    [
        (EXPIRATION_SLOT - 600, EXPIRATION_SLOT - 100),
        (EXPIRATION_SLOT - 48 * 3600, EXPIRATION_SLOT - 48 * 3600 + 100),
    ],
    ids=["before-expiration", "before-the-hour-cap"],
)
def test_fill_keeps_an_earlier_deadline(tip: int, ttl: int) -> None:
    tb = _fill(tip=tip, ttl=ttl)
    assert tb.ttl == ttl


def test_fill_of_a_non_expiring_swap_sets_no_deadline() -> None:
    # Only an expiring swap needs an upper bound; the one-hour window caps it,
    # it does not add one.
    tb = _fill(tip=EXPIRATION_SLOT, expiration=None)
    assert tb.ttl is None


# --- prev_input: the two output-reference shapes ----------------------------

PREV_HASH = bytes.fromhex("ab" * 32)
PREV_INDEX = 5
# Some(OutputReference { transaction_id: ByteArray, output_index: Int }): the
# official contracts' shape.
OFFICIAL_PREV_HEX = "d8799f" + "d8799f" + "5820" + "ab" * 32 + "05" + "ff" + "ff"
OFFICIAL_PREV = CBORTag(121, [CBORTag(121, [PREV_HASH, PREV_INDEX])])
# Some(OutputReference { transaction_id: TransactionId { hash }, output_index }):
# the pre-release build's shape, with the id wrapped in one more Constr 0.
LEGACY_PREV_HEX = (
    "d8799f" + "d8799f" + "d8799f" + "5820" + "ab" * 32 + "ff" + "05" + "ff" + "ff"
)
LEGACY_PREV = CBORTag(121, [CBORTag(121, [CBORTag(121, [PREV_HASH]), PREV_INDEX])])
NONE_HEX = "d87a80"


def test_prev_input_vectors_encode_their_shapes() -> None:
    assert cbor2.loads(bytes.fromhex(OFFICIAL_PREV_HEX)) == OFFICIAL_PREV
    assert cbor2.loads(bytes.fromhex(LEGACY_PREV_HEX)) == LEGACY_PREV


def _datum_hex(prev_hex: str) -> str:
    """An official ADA-for-TOKEN datum's CBOR with ``prev_input`` spliced in.

    The expiration is set, so the resting datum's only ``None`` is ``prev_input``.
    """
    resting = _make_datum(
        b"",
        b"",
        bytes.fromhex("a" * 56),
        bytes.fromhex("414141"),
        2,
        1,
        expiration=CardanoSwapsSomeInt(value=EXPIRATION),
    ).to_cbor_hex()
    assert resting.count(NONE_HEX) == 1
    return resting.replace(NONE_HEX, prev_hex)


@pytest.mark.parametrize(
    ("prev_hex", "some_type", "ref_type"),
    [
        (OFFICIAL_PREV_HEX, CardanoSwapsSomeOutRefV3, CardanoSwapsOutRefV3),
        (LEGACY_PREV_HEX, CardanoSwapsSomeOutRef, CardanoSwapsOutputReference),
    ],
    ids=["official", "legacy"],
)
def test_datum_parses_either_prev_input_shape_byte_exactly(
    prev_hex: str,
    some_type: type,
    ref_type: type,
) -> None:
    datum_hex = _datum_hex(prev_hex)
    datum = CardanoSwapsSwapDatum.from_cbor(datum_hex)
    assert type(datum.prev_input) is some_type
    assert type(datum.prev_input.value) is ref_type
    assert datum.to_cbor_hex() == datum_hex
    assert datum.prev_input_ref() == (PREV_HASH, PREV_INDEX)


def test_prev_input_ref_is_none_for_a_resting_swap() -> None:
    datum = CardanoSwapsSwapDatum.from_cbor(_datum_hex(NONE_HEX))
    assert isinstance(datum.prev_input, PlutusNone)
    assert datum.prev_input_ref() is None


def test_contracts_carry_their_prev_input_shape() -> None:
    assert OFFICIAL_CONTRACT.prev_input_class is CardanoSwapsSomeOutRefV3
    assert LEGACY_CONTRACT.prev_input_class is CardanoSwapsSomeOutRef


@pytest.mark.parametrize(
    ("contract", "prev_hex"),
    [(OFFICIAL_CONTRACT, OFFICIAL_PREV_HEX), (LEGACY_CONTRACT, LEGACY_PREV_HEX)],
    ids=["official", "legacy"],
)
def test_continuation_writes_the_contract_prev_input_shape(contract, prev_hex) -> None:
    resting = CardanoSwapsSwapDatum.from_cbor(_datum_hex(NONE_HEX))
    cont = resting.continuation_datum(PREV_HASH, PREV_INDEX, contract=contract)
    assert cont.prev_input.to_cbor_hex() == prev_hex
    # Every other field is the resting datum's.
    assert cont.to_cbor_hex() == _datum_hex(prev_hex)


def _resting_datum_on(contract) -> CardanoSwapsSwapDatum:
    """A resting ADA-for-TOKEN datum whose ``beacon_id`` is ``contract``'s policy."""
    if contract is LEGACY_CONTRACT:
        return _legacy_state().order_datum
    return CardanoSwapsSwapDatum.from_cbor(_datum_hex(NONE_HEX))


@pytest.mark.parametrize(
    ("own", "prev_hex"),
    [(OFFICIAL_CONTRACT, OFFICIAL_PREV_HEX), (LEGACY_CONTRACT, LEGACY_PREV_HEX)],
    ids=["official", "legacy"],
)
def test_continuation_defaults_to_the_datum_contract_shape(own, prev_hex) -> None:
    cont = _resting_datum_on(own).continuation_datum(PREV_HASH, PREV_INDEX)
    assert cont.prev_input.to_cbor_hex() == prev_hex


def test_contract_for_beacon() -> None:
    assert contract_for_beacon(bytes.fromhex(BEACON_POLICY_ID)) is OFFICIAL_CONTRACT
    assert (
        contract_for_beacon(bytes.fromhex(LEGACY_BEACON_POLICY_ID)) is LEGACY_CONTRACT
    )
    with pytest.raises(ValueError, match="ee" * 28):
        contract_for_beacon(bytes.fromhex("ee" * 28))


@pytest.mark.parametrize(
    ("own", "other"),
    [(OFFICIAL_CONTRACT, LEGACY_CONTRACT), (LEGACY_CONTRACT, OFFICIAL_CONTRACT)],
    ids=["official", "legacy"],
)
def test_datum_sizing_defaults_to_the_datum_contract(own, other) -> None:
    datum = _resting_datum_on(own)
    held = Assets(root={TOKEN: 8_000_000})

    def sizes(**contract):
        return (
            datum.continuation_min_lovelace(held, CPUB, **contract),
            datum.claimable_offer(10_000_000, CPUB, held_ask=1_000, **contract),
            datum.carrier_lovelace(10_000_000, CPUB, **contract),
        )

    assert sizes() == sizes(contract=own)
    # Each size depends on the continuation's prev_input shape.
    assert all(a != b for a, b in zip(sizes(), sizes(contract=other)))


@pytest.mark.parametrize(
    "method",
    [
        "continuation_datum",
        "continuation_min_lovelace",
        "claimable_offer",
        "carrier_lovelace",
    ],
)
def test_datum_with_an_unknown_beacon_id_raises(method) -> None:
    unknown = "ee" * 28
    datum = _resting_datum_on(OFFICIAL_CONTRACT)
    datum.beacon_id = bytes.fromhex(unknown)
    calls = {
        "continuation_datum": lambda **kw: datum.continuation_datum(
            PREV_HASH, PREV_INDEX, **kw
        ),
        "continuation_min_lovelace": lambda **kw: datum.continuation_min_lovelace(
            Assets(root={TOKEN: 8_000_000}), CPUB, **kw
        ),
        "claimable_offer": lambda **kw: datum.claimable_offer(10_000_000, CPUB, **kw),
        "carrier_lovelace": lambda **kw: datum.carrier_lovelace(10_000_000, CPUB, **kw),
    }
    with pytest.raises(ValueError, match=unknown):
        calls[method]()
    # An explicit contract needs no lookup.
    calls[method](contract=OFFICIAL_CONTRACT)


def test_token_offer_with_an_unknown_beacon_id_raises_too() -> None:
    # A token offer sizes no continuation, but an unknown beacon id is refused the
    # same way an ADA offer's is.
    unknown = "ee" * 28
    datum = dataclasses.replace(
        _resting_datum_on(OFFICIAL_CONTRACT),
        beacon_id=bytes.fromhex(unknown),
        offer_id=bytes.fromhex("a" * 56),
        offer_name=bytes.fromhex("414141"),
        ask_id=b"",
        ask_name=b"",
    )
    with pytest.raises(ValueError, match=unknown):
        datum.claimable_offer(5_000, CPUB)
    assert datum.claimable_offer(5_000, CPUB, contract=OFFICIAL_CONTRACT) == 5_000


def test_official_fill_writes_the_official_prev_input() -> None:
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex("a" * 56),
        bytes.fromhex("414141"),
        2,
        1,
        10_000_000,
    )
    tb = TransactionBuilder(_OfflineContext())
    cont_txo, _ = state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={TOKEN: 8_000_000}),
        out_assets=Assets(root={"lovelace": 4_000_000}),
        tx_builder=tb,
        owner_address=OWNER,
    )
    assert cont_txo is not None
    fields = cbor2.loads(cont_txo.datum.to_cbor()).value
    spent = bytes.fromhex(state.tx_hash)
    assert fields[9] == CBORTag(121, [CBORTag(121, [spent, state.tx_index])])
    assert cont_txo.datum.prev_input.to_cbor_hex() == (
        "d8799f" + "d8799f" + "5820" + state.tx_hash + "00" + "ff" + "ff"
    )


def test_continuation_sizing_follows_the_contract_shape() -> None:
    resting = CardanoSwapsSwapDatum.from_cbor(_datum_hex(NONE_HEX))
    held = Assets(root={TOKEN: 8_000_000})
    official = resting.continuation_min_lovelace(held, CPUB, contract=OFFICIAL_CONTRACT)
    legacy = resting.continuation_min_lovelace(held, CPUB, contract=LEGACY_CONTRACT)
    # The pre-release shape's extra Constr 0 around the id: d8799f ... ff.
    assert legacy - official == 4 * CPUB
    assert resting.continuation_min_lovelace(held, CPUB) == official


@pytest.mark.parametrize(
    ("state", "other"),
    [
        (
            _resting_state(
                b"",
                b"",
                bytes.fromhex("a" * 56),
                bytes.fromhex("414141"),
                2,
                1,
                10_000_000,
            ),
            LEGACY_CONTRACT,
        ),
        (_legacy_state(), OFFICIAL_CONTRACT),
    ],
    ids=["official", "legacy"],
)
def test_claimable_offer_is_sized_on_the_class_contract_shape(state, other) -> None:
    def sized(contract):
        return state.order_datum.claimable_offer(
            state.assets["lovelace"],
            CPUB,
            held_ask=state.assets[state.in_unit],
            prev_output_index=state.tx_index,
            contract=contract,
        )

    assert state.claimable_offer(CPUB) == sized(state.CONTRACT)
    assert sized(state.CONTRACT) != sized(other)


def test_legacy_parses_a_filled_swap_datum() -> None:
    prev = CardanoSwapsSomeOutRef(
        value=CardanoSwapsOutputReference(
            transaction_id=CardanoSwapsTxId(tx_hash=PREV_HASH),
            output_index=PREV_INDEX,
        ),
    )
    state = _legacy_state(prev_input=prev)
    assert type(state.order_datum.prev_input) is CardanoSwapsSomeOutRef
    assert state.order_datum.prev_input_ref() == (PREV_HASH, PREV_INDEX)
    assert state.available.quantity() == 10_000_000
