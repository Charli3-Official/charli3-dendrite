"""Shared lending unit, credential and out-ref helpers."""

import cbor2
import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash
from pycardano.exception import DeserializeException

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending import units
from charli3_dendrite.lending.units import DECODE_ERRORS
from charli3_dendrite.lending.units import POLICY_HEX_LEN
from charli3_dendrite.lending.units import canonical_unit_key
from charli3_dendrite.lending.units import names_under
from charli3_dendrite.lending.units import out_ref
from charli3_dendrite.lending.units import out_ref_key
from charli3_dendrite.lending.units import policy_of
from charli3_dendrite.lending.units import script_credential
from charli3_dendrite.lending.units import script_payment_address
from charli3_dendrite.lending.units import sole_token_name

POLICY = "ab" * 28
OTHER = "cd" * 28


def _info(tx_hash: str = "ee" * 32, tx_index: int = 3) -> PoolStateInfo:
    return PoolStateInfo(
        address=script_payment_address(POLICY),
        tx_hash=tx_hash,
        tx_index=tx_index,
        block_time=0,
        block_index=0,
        block_hash="",
        datum_hash="",
        datum_cbor="",
        assets=Assets(root={"lovelace": 1}),
        plutus_v2=False,
    )


def test_policy_hex_len_is_a_28_byte_hash():
    assert POLICY_HEX_LEN == 56


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("lovelace", ""),
        (POLICY, POLICY),
        (POLICY + "01", POLICY),
        (POLICY + "4d494e", POLICY),
    ],
)
def test_policy_of(unit, expected):
    assert policy_of(unit) == expected


def test_canonical_unit_key_matches_the_assets_order():
    units_ = [OTHER, POLICY + "02", "lovelace", POLICY, POLICY + "01"]
    expected = list(Assets(root=dict.fromkeys(units_, 1)).root)
    assert expected[0] == "lovelace"
    assert sorted(units_, key=canonical_unit_key) == expected
    assert canonical_unit_key("lovelace") == ""
    assert canonical_unit_key(POLICY + "01") == POLICY + "01"


def test_names_under_lists_sorted_names_of_one_policy():
    assets = Assets(
        root={
            "lovelace": 5,
            POLICY + "02": 1,
            POLICY: 7,
            POLICY + "01": 2,
            OTHER + "01": 1,
        },
    )
    assert names_under(assets, POLICY) == ["", "01", "02"]
    assert names_under(assets, OTHER) == ["01"]
    assert names_under(assets, "ef" * 28) == []
    assert names_under(assets, "") == []


@pytest.mark.parametrize(
    ("root", "expected"),
    [
        ({"lovelace": 1, POLICY + "ab": 1}, "ab"),
        ({"lovelace": 1, POLICY: 1}, ""),
        ({"lovelace": 1}, None),
        ({"lovelace": 1, OTHER + "ab": 1}, None),
        ({"lovelace": 1, POLICY + "ab": 2}, None),
        ({"lovelace": 1, POLICY: 2}, None),
        ({"lovelace": 1, POLICY + "ab": 1, POLICY + "cd": 1}, None),
        ({"lovelace": 1, POLICY: 1, POLICY + "cd": 1}, None),
    ],
)
def test_sole_token_name(root, expected):
    assert sole_token_name(Assets(root=root), POLICY) == expected


def test_script_credential_reads_a_script_payment_part():
    assert script_credential(script_payment_address(POLICY)) == POLICY


@pytest.mark.parametrize(
    "address",
    [
        Address(
            payment_part=VerificationKeyHash(bytes.fromhex(POLICY)),
            network=Network.MAINNET,
        ).encode(),
        "",
        "junk",
        "stake1uy87e94pljg7sawrg8jz98mmup2nqf3gmv6p8cf6sdjaklcpy5vea",
        "addr1vx4ssmpdn3",
        "addr1sq3q9yudue",
    ],
)
def test_script_credential_is_none_for_anything_else(address):
    assert script_credential(address) is None


def test_decode_errors_cover_truncated_cbor_and_bad_values():
    for exc in (
        cbor2.CBORDecodeError,
        DeserializeException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
    ):
        assert exc in DECODE_ERRORS
    with pytest.raises(DECODE_ERRORS):
        cbor2.loads(bytes.fromhex("d87983"))


def test_out_ref_and_its_key():
    assert out_ref(_info("ee" * 32, 3)) == "ee" * 32 + "#3"
    assert out_ref_key("ee" * 32 + "#3") == ("ee" * 32, 3)
    assert out_ref_key("aa#10") > out_ref_key("aa#9")
    assert out_ref_key("aa#x") == ("aa", -1)
    assert out_ref_key("aa") == ("aa", -1)


def test_old_homes_reexport_the_shared_helpers():
    from charli3_dendrite.lending.fluidtokens_v4 import indexing as ft_indexing
    from charli3_dendrite.lending.fluidtokens_v4 import state as ft_state

    assert ft_indexing.script_credential is units.script_credential
    assert ft_indexing.DECODE_ERRORS is units.DECODE_ERRORS
    assert ft_state.names_under is units.names_under


@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("indexing", ("DECODE_ERRORS", "script_credential", "sole_token_name")),
        ("lifecycle", ("script_credential", "policy_of", "out_ref")),
        ("deployment", ("DECODE_ERRORS", "policy_of", "out_ref")),
        ("market", ("canonical_unit_key", "policy_of")),
        ("oracles", ("out_ref_key",)),
    ],
)
def test_liqwid_modules_use_the_shared_helpers(module, names):
    import importlib

    mod = importlib.import_module(f"charli3_dendrite.lending.liqwid.{module}")
    for name in names:
        assert getattr(mod, name) is getattr(units, name), (module, name)


@pytest.mark.parametrize(
    ("module", "private"),
    [
        ("charli3_dendrite.lending.normalized", "_POLICY_HEX_LEN"),
        ("charli3_dendrite.lending.fluidtokens_v4.state", "_POLICY_HEX_LEN"),
        ("charli3_dendrite.lending.liqwid.deployment", "_POLICY_HEX_LEN"),
        ("charli3_dendrite.lending.liqwid.lifecycle", "_POLICY_HEX"),
        ("charli3_dendrite.lending.liqwid.market", "_unit_order"),
        ("charli3_dendrite.lending.liqwid.oracles", "_out_ref_key"),
    ],
)
def test_no_local_copy_of_a_shared_helper_remains(module, private):
    import importlib

    assert not hasattr(importlib.import_module(module), private)
