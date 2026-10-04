"""Official and legacy Cardano-Swaps one-way contract descriptions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
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
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsRational
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSwapDatum
from charli3_dendrite.dexs.ob.cardanoswaps import ask_beacon_name
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


def _legacy_state(offer_qty: int = 10_000_000) -> CardanoSwapsLegacyOrderState:
    """A resting swap on the pre-release build: ADA offered for TOKEN at 2/1."""
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
        prev_input=PlutusNone(),
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


def test_fill_of_an_expiring_swap_sets_its_deadline() -> None:
    expiration = 1_790_000_040_000  # a 60 000 ms multiple
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex("a" * 56),
        bytes.fromhex("414141"),
        2,
        1,
        10_000_000,
    )
    datum = state.order_datum
    datum.expiration = CardanoSwapsSomeInt(value=expiration)
    state.datum_cbor = datum.to_cbor_hex()
    tb = TransactionBuilder(_OfflineContext())
    state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={TOKEN: 8_000_000}),
        out_assets=Assets(root={"lovelace": 4_000_000}),
        tx_builder=tb,
        owner_address=OWNER,
    )
    assert tb.ttl == posix_ms_to_slot(expiration)


def test_fill_keeps_an_earlier_deadline() -> None:
    expiration = 1_790_000_040_000
    state = _resting_state(
        b"",
        b"",
        bytes.fromhex("a" * 56),
        bytes.fromhex("414141"),
        2,
        1,
        10_000_000,
    )
    datum = state.order_datum
    datum.expiration = CardanoSwapsSomeInt(value=expiration)
    state.datum_cbor = datum.to_cbor_hex()
    tb = TransactionBuilder(_OfflineContext())
    tb.ttl = posix_ms_to_slot(expiration) - 100
    state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={TOKEN: 8_000_000}),
        out_assets=Assets(root={"lovelace": 4_000_000}),
        tx_builder=tb,
        owner_address=OWNER,
    )
    assert tb.ttl == posix_ms_to_slot(expiration) - 100


def test_fill_of_a_non_expiring_swap_sets_no_deadline() -> None:
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
    state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={TOKEN: 8_000_000}),
        out_assets=Assets(root={"lovelace": 4_000_000}),
        tx_builder=tb,
        owner_address=OWNER,
    )
    assert tb.ttl is None
