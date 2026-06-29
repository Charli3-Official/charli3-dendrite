from pycardano import IndefiniteList

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import OwnerNft
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import PRational
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.datums import TupleAsset


def test_tuple_asset_unit_ada_is_lovelace():
    assert TupleAsset(policy=b"", name=b"").unit() == "lovelace"
    ta = TupleAsset(policy=bytes.fromhex("aa" * 28), name=b"USD")
    assert ta.unit() == "aa" * 28 + b"USD".hex()


def test_protocol_datum_roundtrip():
    d = ProtocolDatum(
        pool_skh=bytes.fromhex("11" * 28),
        loan_skh=bytes.fromhex("22" * 28),
        config_pool_skh=bytes.fromhex("33" * 28),
        oracle_skh=bytes.fromhex("44" * 28),
    )
    assert ProtocolDatum.from_cbor(d.to_cbor_hex()) == d


def test_pool_datum_roundtrip_and_fields():
    d = PoolDatum(
        total_supply=5_000_000,
        circulating_dtoken=4_000_000,
        total_borrow=1_000_000,
        borrow_apy=400,
        undistributed_fee=10,
        interest_index=1_000_000_000_000,
        interest_time=1_700_000_000_000,
        alt_supply_tokens_rate=[PRational(num=0, denom=1)],
    )
    back = PoolDatum.from_cbor(d.to_cbor_hex())
    assert back.interest_index == 1_000_000_000_000
    assert back.alt_supply_tokens_rate[0].denom == 1
    assert back == d


def test_loan_datum_roundtrip_and_units():
    d = LoanDatum(
        owner_nft=OwnerNft(asset=IndefiniteList([bytes.fromhex("55" * 28), b"own"])),
        token=IndefiniteList([b"", b""]),
        loan_amount=1_000_000,
        initial_interest_index=1_000_000_000_000,
    )
    back = LoanDatum.from_cbor(d.to_cbor_hex())
    assert back == d
    assert back.token_unit() == "lovelace"
    assert back.owner_nft.unit() == "55" * 28 + b"own".hex()
    assert back.loan_amount == 1_000_000
