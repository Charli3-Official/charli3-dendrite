"""Cardano-Swaps two-way contract: constants, beacon names and redeemers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pycardano import Network
from pycardano import PlutusV3Script
from pycardano import ScriptHash
from pycardano import plutus_script_hash

from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRefV3
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithMint
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithStake
from charli3_dendrite.dexs.ob.cardanoswaps import offer_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import (
    TWO_WAY_BEACON_POLICY_SCRIPT_HEX,
)
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_CONTRACT
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_SWAP_VALIDATOR_HASH
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import (
    TWO_WAY_SWAP_VALIDATOR_SCRIPT_HEX,
)
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TakeAsset1
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TakeAsset2
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import sort_pair
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import two_way_asset_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import two_way_pair_beacon_name
from charli3_dendrite.utility import apply_params_to_script

DATA = Path(__file__).parent / "data"

USDM_ID = bytes.fromhex("c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad")
USDM_NAME = bytes.fromhex("0014df105553444d")
USDM_UNIT = USDM_ID.hex() + USDM_NAME.hex()
TEST_POLICY = bytes.fromhex("00112233445566778899aabbccddeeff00112233445566778899aabb")
TEST_NAME = bytes.fromhex("444a4544")
MAX_NAME = bytes.fromhex("ffeeddccbbaa99887766554433221100" * 2)

# Produced by the contract's own Aiken code (v2.0.0 ``two_way_swap/utils.ak``
# generate_asset_beacon / generate_pair_beacon).
ASSET_VECTORS = [
    (
        (b"", b""),
        "07d5f63e85046b83e1fc4102a7c19c3f1711c51984725e3b6cf195900947cebe",
    ),
    (
        (TEST_POLICY, TEST_NAME),
        "2dab55ec954afe698b6de75d81f0067fd503d1e4ecd49d593f8a3b9c9a963d4f",
    ),
    (
        (TEST_POLICY, MAX_NAME),
        "130f1a4999dcc8b6bb9f31949ee44381e587ace2f03437c65d15536770704324",
    ),
    (
        (b"123", b"ABC"),
        "421118aa9387559826262aa7545bfac75f5393f4867a8bb5ab0df111e96aeb15",
    ),
    (
        (USDM_ID, USDM_NAME),
        "a01febd063c6919f19ecafd3114e1203139ba7e8d310672e692b75712ddbdb98",
    ),
]
PAIR_VECTORS = [
    (
        (b"", b"", TEST_POLICY, TEST_NAME),
        "c6656f7142e3ef8e72066251cac9e9cc2454571204fa85ab13b34a03e77d7230",
    ),
    (
        (b"12", b"3", b"AB", b"C"),
        "0980838c69e2b4193015eed4505d222849ecca60fdf069f75a57f250c1762d61",
    ),
    (
        (b"AB", b"", b"CD", b""),
        "6e90ae87cd390983bb293f04b40d22c04fe00c0957eca07f68d63e63d54a005e",
    ),
    (
        (b"AB", b"C", b"D", b""),
        "97bd089a14e634317d6251349086ede4eaff43878495246e925b78bfb8534224",
    ),
    (
        (b"", b"", USDM_ID, USDM_NAME),
        "3bd868e153a76d170be2f6356551d19c1349308d04e0a24be653b98f1efa4fe7",
    ),
]


@pytest.mark.parametrize(("args", "expected"), ASSET_VECTORS)
def test_asset_beacon_names_match_the_contract(args, expected) -> None:
    assert two_way_asset_beacon_name(*args).hex() == expected


@pytest.mark.parametrize(("args", "expected"), PAIR_VECTORS)
def test_pair_beacon_names_match_the_contract(args, expected) -> None:
    assert two_way_pair_beacon_name(*args).hex() == expected


def test_asset_beacon_equals_the_one_way_offer_beacon() -> None:
    # Same pre-image encoding (constructor 0 over two bytestrings); the
    # beacons differ only by minting policy.
    assert two_way_asset_beacon_name(USDM_ID, USDM_NAME) == offer_beacon_name(
        USDM_ID, USDM_NAME
    )


@pytest.mark.parametrize(
    "pair",
    [
        (TEST_POLICY, TEST_NAME, b"", b""),
        (TEST_POLICY, TEST_NAME, TEST_POLICY, TEST_NAME),
        (TEST_POLICY, b"B", TEST_POLICY, b"A"),
    ],
)
def test_pair_beacon_rejects_unsorted_or_equal(pair) -> None:
    with pytest.raises(ValueError):
        two_way_pair_beacon_name(*pair)


def test_sort_pair_puts_lovelace_first_then_policy_then_name() -> None:
    assert sort_pair(USDM_UNIT, "lovelace") == ("lovelace", USDM_UNIT)
    assert sort_pair("lovelace", USDM_UNIT) == ("lovelace", USDM_UNIT)
    test_unit = TEST_POLICY.hex() + TEST_NAME.hex()
    assert sort_pair(USDM_UNIT, test_unit) == (test_unit, USDM_UNIT)
    # Same policy: names compare as bytes, a prefix sorts first.
    short = TEST_POLICY.hex() + "44"
    longer = TEST_POLICY.hex() + "4400"
    other = TEST_POLICY.hex() + "45"
    assert sort_pair(other, longer) == (longer, other)
    assert sort_pair(longer, short) == (short, longer)
    assert sort_pair(TEST_POLICY.hex(), short) == (TEST_POLICY.hex(), short)
    with pytest.raises(ValueError):
        sort_pair(USDM_UNIT, USDM_UNIT)
    with pytest.raises(ValueError):
        sort_pair("lovelace", "lovelace")


def test_redeemer_constructor_indices() -> None:
    assert [
        r().to_cbor_hex()
        for r in (SpendWithMint, SpendWithStake, TakeAsset1, TakeAsset2)
    ] == ["d87980", "d87a80", "d87b80", "d87c80"]


def test_two_way_scripts_are_the_v2_0_0_blueprint() -> None:
    blueprint = json.loads(
        (DATA / "cardanoswaps_official_two_way_scripts.json").read_text()
    )
    assert TWO_WAY_SWAP_VALIDATOR_SCRIPT_HEX == blueprint["swap_validator_hex"]
    assert TWO_WAY_BEACON_POLICY_SCRIPT_HEX == blueprint["beacon_policy_hex"]
    assert (
        TWO_WAY_SWAP_VALIDATOR_HASH
        == "569431d7c48bebb283077d46cbc72bd1f9f57f94c3b6d700aa684095"
    )
    assert (
        TWO_WAY_BEACON_POLICY_ID
        == "ae19cf56a7631068aa754327e0472840b76e485868a324904c8b379e"
    )
    assert TWO_WAY_CONTRACT.script_class is PlutusV3Script
    assert TWO_WAY_CONTRACT.prev_input_class is CardanoSwapsSomeOutRefV3
    assert (
        plutus_script_hash(TWO_WAY_CONTRACT.swap_script()).payload.hex()
        == TWO_WAY_SWAP_VALIDATOR_HASH
    )
    assert (
        plutus_script_hash(TWO_WAY_CONTRACT.beacon_script()).payload.hex()
        == TWO_WAY_BEACON_POLICY_ID
    )
    # The beacon policy is the unapplied blueprint with ``dapp_hash`` (the swap
    # validator hash) applied.
    applied = apply_params_to_script(
        bytes.fromhex(blueprint["beacon_unapplied_hex"]),
        bytes.fromhex(TWO_WAY_SWAP_VALIDATOR_HASH),
    )
    assert applied.hex() == TWO_WAY_BEACON_POLICY_SCRIPT_HEX
    assert str(plutus_script_hash(PlutusV3Script(applied))) == TWO_WAY_BEACON_POLICY_ID


def test_beacon_reward_address_is_the_beacon_script_credential() -> None:
    address = TWO_WAY_CONTRACT.beacon_reward_address(Network.MAINNET)
    assert address.payment_part is None
    assert address.staking_part == ScriptHash(bytes.fromhex(TWO_WAY_BEACON_POLICY_ID))
    assert (
        address.encode()
        == "stake17xhpnn6k5a33q692w4pj0cz89pqtwmjgtp52xfysfj9n08s92lu9k"
    )
