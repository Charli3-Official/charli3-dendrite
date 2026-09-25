"""Classifying raw UTxO records into FluidTokens V4 states (offline)."""

import cbor2
import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LockedBorrowerManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.indexing import parse_utxo
from charli3_dendrite.lending.fluidtokens_v4.indexing import script_credential
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4AssetManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LenderManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LoanState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LockedBorrowerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4RequestState
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.fluidtokens_v4.records import FIX
from tests.lending.fluidtokens_v4.records import record_info
from tests.lending.fluidtokens_v4.records import v4_request_record

EXPECTED_CLASS = {
    "pool": FluidV4PoolState,
    "pool_manager": FluidV4PoolManagerState,
    "loan": FluidV4LoanState,
    "asset_manager": FluidV4AssetManagerState,
    "lender_manager": FluidV4LenderManagerState,
}


@pytest.mark.parametrize(("kind", "cls"), EXPECTED_CLASS.items())
def test_every_live_utxo_parses_as_its_kind(kind, cls):
    for rec in FIX[kind]:
        state = parse_utxo(record_info(rec))
        assert isinstance(state, cls), rec["out_ref"]
        assert state.out_ref == rec["out_ref"]


def test_request_parses_at_the_request_credential():
    state = parse_utxo(record_info(v4_request_record()))
    assert isinstance(state, FluidV4RequestState)
    assert state.request_id == "cd" * 28


def test_locked_borrower_parses_at_its_credential():
    datum = LockedBorrowerManagerDatum(
        origin_ref=TxOutRef(tx_id=bytes.fromhex("aa" * 32), index=1),
        borrower_auth=AuthCardanoSignature(key_hash=bytes.fromhex("bb" * 28)),
    )
    bond = c.BORROWER_BOND_POLICY + "cc" * 28
    state = parse_utxo(
        record_info(
            FIX["pool"][0],
            address=script_payment_address(c.LOCKED_BORROWER_MANAGER_SPEND_SKH),
            datum_cbor=datum.to_cbor_hex(),
            assets=Assets(root={"lovelace": 2_000_000, bond: 1}),
        ),
    )
    assert isinstance(state, FluidV4LockedBorrowerState)
    assert state.origin_out_ref == "aa" * 32 + "#1"


def test_selectors_follow_the_config():
    selectors = entity_selectors()
    assert set(selectors) == set(EntityKind)
    assert selectors[EntityKind.POOL].payment_credential == c.POOL_SPEND_SKH
    assert selectors[EntityKind.POOL].identity_policy == c.POOL_POLICY
    assert selectors[EntityKind.LENDER_MANAGER].identity_policy is None
    for kind, rec in (("pool", FIX["pool"][0]), ("loan", FIX["loan"][0])):
        selector = selectors[EntityKind(kind)]
        assert script_credential(selector.address) == script_credential(rec["address"])

    redeployed = ConfigDatum.from_cbor(FIX["config"][-1]["datum_cbor"])
    redeployed.pool_spend_script_hash = bytes.fromhex("11" * 28)
    moved = entity_selectors(redeployed)[EntityKind.POOL]
    assert moved.payment_credential == "11" * 28
    assert parse_utxo(record_info(FIX["pool"][0]), entity_selectors(redeployed)) is None


def test_pool_without_its_nft_is_not_a_pool():
    rec = FIX["pool"][0]
    stripped = {
        u: q for u, q in rec["assets"].items() if not u.startswith(c.POOL_POLICY)
    }
    assert parse_utxo(record_info(rec, assets=Assets(root=stripped))) is None


def test_pool_with_two_pool_nfts_is_not_a_pool():
    rec = FIX["pool"][0]
    doubled = dict(rec["assets"])
    doubled[c.POOL_POLICY + "ff" * 29] = 1
    assert parse_utxo(record_info(rec, assets=Assets(root=doubled))) is None


def test_pool_nft_with_quantity_two_is_not_a_pool():
    rec = FIX["pool"][0]
    minted = {
        u: (2 if u.startswith(c.POOL_POLICY) else q) for u, q in rec["assets"].items()
    }
    assert parse_utxo(record_info(rec, assets=Assets(root=minted))) is None


def test_foreign_credential_is_ignored():
    rec = FIX["pool"][0]
    wallet = Address(
        payment_part=VerificationKeyHash(b"\x01" * 28),
        network=Network.MAINNET,
    ).encode()
    assert parse_utxo(record_info(rec, address=wallet)) is None


def test_key_hash_with_a_script_hash_payload_is_ignored():
    # Every V4 entity sits at a script credential; a key hash with the same bytes is
    # a different credential.
    rec = FIX["pool"][0]
    key_address = Address(
        payment_part=VerificationKeyHash(bytes.fromhex(c.POOL_SPEND_SKH)),
        network=Network.MAINNET,
    ).encode()
    assert parse_utxo(record_info(rec, address=key_address)) is None


@pytest.mark.parametrize(
    "address",
    [
        "",
        "junk",
        "stake1uy87e94pljg7sawrg8jz98mmup2nqf3gmv6p8cf6sdjaklcpy5vea",
        # Valid bech32 checksums: a one-byte key hash, and a Byron header.
        "addr1vx4ssmpdn3",
        "addr1sq3q9yudue",
    ],
)
def test_unparseable_address_is_ignored(address):
    assert script_credential(address) is None
    assert parse_utxo(record_info(FIX["pool"][0], address=address)) is None


def test_missing_inline_datum_is_ignored():
    assert parse_utxo(record_info(FIX["pool"][0], datum_cbor="")) is None


@pytest.mark.parametrize(
    "junk",
    ["zz", "00", "d87980", "ffffff", "d87a", "9f", "d8799f01"],
)
def test_junk_datum_is_ignored(junk):
    assert parse_utxo(record_info(FIX["pool"][0], datum_cbor=junk)) is None


def test_datum_of_another_kind_is_ignored():
    # A copied pool-manager datum at the pool credential, with the pool NFT present.
    rec = FIX["pool"][0]
    foreign = FIX["pool_manager"][0]["datum_cbor"]
    assert parse_utxo(record_info(rec, datum_cbor=foreign)) is None


def test_manager_with_a_malformed_untyped_field_is_ignored():
    # Lender managers have no identity token; a decodable datum with a malformed
    # authorization field must still be rejected at parse time.
    rec = FIX["lender_manager"][0]
    top = cbor2.loads(bytes.fromhex(rec["datum_cbor"]))
    fields = list(top.value)
    fields[0] = cbor2.CBORTag(121, [7])
    bad = cbor2.dumps(cbor2.CBORTag(top.tag, fields)).hex()
    assert parse_utxo(record_info(rec, datum_cbor=bad)) is None


BAD_VALUES = [
    b"\x00",
    7,
    cbor2.CBORTag(121, []),
    [],
    {1: 2},
    cbor2.CBORTag(125, [1, 2, 3]),
]


@pytest.mark.parametrize("kind", EXPECTED_CLASS)
def test_parse_never_raises_on_corrupted_fields(kind):
    rec = FIX[kind][0]
    top = cbor2.loads(bytes.fromhex(rec["datum_cbor"]))
    for index in range(len(top.value)):
        for bad in BAD_VALUES:
            fields = list(top.value)
            fields[index] = bad
            cbor_hex = cbor2.dumps(cbor2.CBORTag(top.tag, fields)).hex()
            parse_utxo(record_info(rec, datum_cbor=cbor_hex))
