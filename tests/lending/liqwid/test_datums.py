"""Liqwid datum readers and record decoders against real mainnet CBOR.

``fixtures/mainnet_datums.json`` holds single datums for the original readers;
``fixtures/records.json.gz`` (read through ``records_of``) holds the captured records
of every kind and historical layout.
"""

import dataclasses
import decimal
import json
from fractions import Fraction
from pathlib import Path

import cbor2
import pytest

from charli3_dendrite.lending.liqwid.datums import ACTION_LENGTHS
from charli3_dendrite.lending.liqwid.datums import COLLATERAL_PARAMS_LENGTHS
from charli3_dendrite.lending.liqwid.datums import LIQWID_DECODE_ERRORS
from charli3_dendrite.lending.liqwid.datums import LOAN_LENGTHS
from charli3_dendrite.lending.liqwid.datums import MARKET_PARAMS_LENGTHS
from charli3_dendrite.lending.liqwid.datums import MARKET_STATE_LENGTHS
from charli3_dendrite.lending.liqwid.datums import ORACLE_FEED_LENGTHS
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import OracleFeedDatum
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.datums import decode_collateral_params
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.datums import decode_market_params
from charli3_dendrite.lending.liqwid.datums import decode_market_state
from charli3_dendrite.lending.liqwid.datums import decode_oracle_feed
from charli3_dendrite.lending.liqwid.datums import field_count
from charli3_dendrite.lending.liqwid.datums import liqwid_oracle_feed_id
from charli3_dendrite.lending.liqwid.datums import liqwid_qtoken_rate
from charli3_dendrite.lending.liqwid.datums import parse_liqwid_market_param
from charli3_dendrite.lending.liqwid.datums import parse_liqwid_market_state
from charli3_dendrite.lending.liqwid.datums import parse_liqwid_oracle_v2
from charli3_dendrite.lending.units import DECODE_ERRORS
from tests.lending.liqwid.records import records_of

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "mainnet_datums.json").read_text()
)


def test_liqwid_oracle_v2_parses_priced_asset_and_rate():
    # ADA feed: field 1 is the empty TupleAsset, field 2 the [num, denom] rate, and
    # field 0 the oracle updater's key.
    datum = FIX["oracle_v2"]["datum"]
    assert parse_liqwid_oracle_v2(datum) == ("lovelace", (16271, 100000))
    assert (
        liqwid_oracle_feed_id(datum)
        == "49afdaa6689e72e3f96ddac9872fad0ee2503fc3268a8868c17eee1f"
    )


def test_liqwid_market_param_surfaces_initial_qtoken_rate_and_max_ltv():
    # Interest-model / risk config record (not a price source).
    datum = FIX["market_param"]["datum"]
    initial_qtoken_rate, max_ltv = parse_liqwid_market_param(datum)
    assert initial_qtoken_rate == (1, 50)
    assert max_ltv == (8696, 10000)


def test_liqwid_market_state_reproduces_qtoken_price():
    # Exchange rate at state field 9; qToken price in ADA = rate / 100 / ada_usd,
    # with ada_usd = 3133/12500 from the sibling ADA oracle.
    datum = FIX["market_state"]["datum"]
    num, denom = parse_liqwid_market_state(datum)
    assert (num, denom) == (360073866973321, 12033383669199582)
    ada_usd = Fraction(3133, 12500)
    qtoken_price = Fraction(num, denom) / 100 / ada_usd
    assert qtoken_price == Fraction(45009233371665125, 37700591035602290406)


def test_liqwid_market_state_zero_rate_rejected():
    # 10-element array: ints 0..8 then a zero rational [0,1] at index 9.
    zero = "9f0001020304050607089f0001ffff"
    with pytest.raises(ValueError):
        parse_liqwid_market_state(zero)


def test_liqwid_qtoken_rate_passthrough():
    assert liqwid_qtoken_rate(
        qtoken_rate_num=1_050_000, qtoken_rate_denom=1_000_000
    ) == (1_050_000, 1_000_000)


def test_liqwid_qtoken_zero_denom_rejected():
    with pytest.raises(ValueError):
        liqwid_qtoken_rate(qtoken_rate_num=1, qtoken_rate_denom=0)


# --- Whole-record decoders ----------------------------------------------------------
# Captured mainnet records come from ``fixtures/records.json.gz`` (``records_of``); a few
# single datums are inlined verbatim from the chain.

LIST27 = "36d4bfe57e1ebd90e0e586f228b84903403e72c9d6d54b88994f66a0372f18ee#1"
LIST30 = "8eb900a137226047109f92f1bd1c73d18d3ce1b55ba621345ffc9acb727d9f56#0"
LIST32 = "e51f0164dba6c22842e7ae8f539582c2e5b425ea6ac2e824beaa7bfb29665fba#3"
LIST34 = "6e8dab3ab0d52a00f848ec209ec6fd37504afa4076d1bbfc5abccf85d061d9da#10"
LIST36 = "52474aff233de3dc5a75b80de64ceb4ae63aabb99cd7bd408c51349979ed9a2b#0"
LAYOUTS = {LIST27: 27, LIST30: 30, LIST32: 32, LIST34: 34, LIST36: 36}
PARKED_PARAMS = "88d368eaf1347e4b8643e17983dff1826252b38e0bc740f7c65bd2d7c3651f62#0"
GENESIS_ADA_COLLATERAL = (
    "36d4bfe57e1ebd90e0e586f228b84903403e72c9d6d54b88994f66a0372f18ee#0"
)
SCRIPT_CLASS_COLLATERAL = (
    "4e7f302abdf8d778fc916126db0748d196c804267b0a7f2804d5229e77d5137b#29"
)
GENESIS_ADA_STATE = "36d4bfe57e1ebd90e0e586f228b84903403e72c9d6d54b88994f66a0372f18ee#2"
ADA_NFT_LIST0 = "1d3ae09325f621351854283e96939f696a8afdeeeac4ab0562f368809d0c3175#0"
ADA_NFT_LIST27 = "7612bcd18e85052b957101c47cf11fbba31d5bc752c6c0a96c329ac7d68b9e0d#0"

# The first Ada loan (b37a8161...#1, a legacy validator).
FIRST_ADA_LOAN = (
    "9f581c23c820c49b92eb4b6cce4aded5baddb9efe5b884fcc93a1e5834b1c41a06052340001a"
    "00013ba01b003b9aca00000000ff"
)
# The first oracle feed (9f437f1e...#0): Ada, list5 at the V1 oracle validator.
FIRST_ADA_FEED = (
    "9f581ccfe224295a282d69edda5fa8de4f131e2b9cd21a6c9235597fa4ff6bd87a9f9f4040ff"
    "ff9f18bd1901f4ff1b000001860f94652619ea60ff"
)
# AGIX at the hard-caps validator (4ddadebb...#0): list6, field 5 = caps.
AGIX_FEED = (
    "9f581c2f97c124ebe90d23fac1be4111b5fda977021cb3f88cbe5d87145b61d87a9f9f581cf4"
    "3a62fdc3965df486de8a0d32fe800963589c41b38946602a0dc5354441474958ffff9f1a0001"
    "1a591a00989680ff1b0000019229ea9eb91a000dbba0d8799f9f9f011903e8ff9f0118c8ffff"
    "ffff"
)
# Pegs at the hard-coded validator: DJED list5 (734fe82a...#0), USDA list6
# (e0e46965...#0).
DJED_PEG_LIST5 = (
    "9f581ccfe224295a282d69edda5fa8de4f131e2b9cd21a6c9235597fa4ff6bd87a9f9f581c8d"
    "b269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd614c446a65644d6963726f55"
    "5344ffff9f0101ff1b000001867a4ac21419ea60ff"
)
USDA_PEG_LIST6 = (
    "9f581c6fa6388ff21b8b8c76966d3e920f6ac8f0c9fb450d30eb51f85c660ad87a9f9f581cfe"
    "7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae4564455534441ffff9f0101ff"
    "1b00000195664a96181a000dbba0d87a80ff"
)
ADA_LOAN_VALIDATOR = "71391f18fb131f28a230fa7f3b6c6099e447602b2bd2df5d046c5e99"
ADA_QTOKEN = "a04ce7a52545e5e33c2867e148898d9e667a69602285f6a1298f9d68"
ADA_MARKET_STATE_NFT = "5a3cb4f52fb3a00ab5abe62080618623811ccafab9c760d0b686e44c"
POLICY_WIDE_POLICY = "1f3dd1b685f0d87eaf92120aca3cf56da6ed91e2dbc32a95a26bce4a"
UPDATER = "6fa6388ff21b8b8c76966d3e920f6ac8f0c9fb450d30eb51f85c660a"
# Each decoder and the record name its error messages start with.
# A general constructor (tag 102) whose alternative is an infinite float, as field 0
# of a five-field list.
INFINITE_TAG = cbor2.CBORTag(102, [float("inf"), []])
INFINITE_ALTERNATIVE = cbor2.dumps([INFINITE_TAG, 1, 2, 3, 4]).hex()
# A five-field list whose first field is 20,000 nested one-element arrays.
DEEPLY_NESTED = (b"\x85" + b"\x81" * 20_000 + b"\x00" + b"\x01\x02\x03\x04").hex()

DECODERS = {
    decode_market_state: "market state",
    decode_market_params: "market params",
    decode_collateral_params: "collateral params",
    decode_loan: "loan",
    decode_oracle_feed: "oracle feed",
    decode_action: "action",
}


def _record(kind: str, out_ref: str) -> dict:
    """The captured record ``out_ref`` of ``kind``."""
    for rec in records_of(kind):
        if rec["out_ref"] == out_ref:
            return rec
    pytest.fail(f"{kind} {out_ref} missing from records.json.gz")


def _datum(kind: str, out_ref: str) -> str:
    return _record(kind, out_ref)["datum_cbor"]


def _genuine(kind: str) -> list[dict]:
    """The captured genuine records of ``kind`` (labelled non-records excluded)."""
    return [rec for rec in records_of(kind) if rec["label"] is None]


def _non_records(kind: str) -> list[dict]:
    """The labelled non-records captured at the credentials of ``kind``."""
    return [rec for rec in records_of(kind) if rec["label"] is not None]


def _definite(datum: str) -> str:
    """The same value re-encoded with definite-length arrays only."""
    return cbor2.dumps(cbor2.loads(bytes.fromhex(datum))).hex()


def _indefinite_bytes(obj: object) -> bytes:
    if isinstance(obj, list):
        return b"\x9f" + b"".join(_indefinite_bytes(x) for x in obj) + b"\xff"
    if isinstance(obj, cbor2.CBORTag):
        # The tag head alone: encode the tag around ``None`` (0xf6) and drop it.
        return cbor2.dumps(cbor2.CBORTag(obj.tag, None))[:-1] + _indefinite_bytes(
            obj.value,
        )
    return cbor2.dumps(obj)


def _indefinite(datum: str) -> str:
    """The same value re-encoded with indefinite-length arrays only."""
    return _indefinite_bytes(cbor2.loads(bytes.fromhex(datum))).hex()


def _encode(fields: list) -> str:
    """A definite-list record carrying ``fields`` (for synthetic variants)."""
    return cbor2.dumps(fields).hex()


def _fields(datum: str) -> list:
    return list(cbor2.loads(bytes.fromhex(datum)))


def _shape(obj: object) -> object:
    """A value's CBOR shape: types and constructor tags, not contents."""
    if isinstance(obj, cbor2.CBORTag):
        return ("tag", obj.tag, _shape(obj.value))
    if isinstance(obj, list):
        return [_shape(x) for x in obj]
    return type(obj).__name__


def test_record_lengths_are_the_known_layouts():
    assert frozenset({27, 30, 32, 34, 36}) == MARKET_PARAMS_LENGTHS
    assert frozenset({11}) == MARKET_STATE_LENGTHS
    assert frozenset({5}) == COLLATERAL_PARAMS_LENGTHS == LOAN_LENGTHS
    assert frozenset({5, 6}) == ORACLE_FEED_LENGTHS
    assert frozenset({2}) == ACTION_LENGTHS


@pytest.mark.parametrize(("out_ref", "length"), sorted(LAYOUTS.items()))
def test_market_params_every_layout_decodes_with_its_length(out_ref, length):
    params = decode_market_params(_datum("market_params", out_ref))
    assert params.length == length
    assert len(params.raw) == length


def test_market_params_list27_genesis_ada_values():
    params = decode_market_params(_datum("market_params", LIST27))
    assert params == MarketParamsDatum(
        length=27,
        initial_qtoken_rate=(1, 50),
        income_ratio=(20, 15, 0, 3),
        legacy_loan_validator=ADA_LOAN_VALIDATOR,
        interest_period_ms=2_000_000,
        rate_model=(
            (2827979481, 2251799813685248),
            (63733912065, 4503599627370496),
            (277909087937, 2251799813685248),
            (13, 20),
        ),
        max_ltv=(3, 4),
        max_collaterals=3,
        min_interest_rate=(1, 1250),
        close_factor=(3, 4),
        liquidation_threshold=(4, 5),
        action_validator="0faa4f20b9d810205f89b42896e693ffc89c3ee4e307f4f0a4893e13",
        batch_validator="c0588460f21b1474de8f7621b3c90572ce05ee08e22b006bb2f21f64",
        borrow_cap=None,
        supply_cap=None,
        withdraw_zero=None,
        loan_validator=None,
        raw=(),
    )


def test_market_params_list36_usdrf_values():
    params = decode_market_params(_datum("market_params", LIST36))
    assert params == MarketParamsDatum(
        length=36,
        initial_qtoken_rate=(1, 50),
        income_ratio=(10, 9, 0, 0),
        legacy_loan_validator="5b9cdc9f1058fd2f3757789677740200ebd2cbfa06c6a8f211b7e128",
        interest_period_ms=2_000_000,
        rate_model=(
            (316307858, 1000000000000000),
            (5068482097, 1000000000000000),
            (159530366082, 1000000000000000),
            (9, 10),
        ),
        max_ltv=(8696, 10000),
        max_collaterals=5,
        min_interest_rate=(1, 1250),
        close_factor=(1, 1),
        liquidation_threshold=(1, 1),
        action_validator="99bf8c3c9037c5ebee0e72e18e3d28fd5d0c382b4d9e6db4b31d518d",
        batch_validator="532a2d8680d0e5d8045e0357b6b45d01f7b7dbc9e8bc0b314ef9c1f1",
        borrow_cap=None,
        supply_cap=10_000_000_000_000,
        withdraw_zero=(
            "e22cc35ebdc7dd370c68e99af0dc0493977dda2a29c3214c35836b7f",
            "38329ef79166dc59870c435773b7f46818f562ccdf7395a49ef485b5",
        ),
        loan_validator="c62d90f088202624e729f55329405d6445114e06aba90f31c1f7126f",
        raw=(),
    )


def test_borrow_cap_is_read_only_from_p32_of_list34_and_list36():
    decoded = {
        ref: decode_market_params(_datum("market_params", ref)) for ref in LAYOUTS
    }
    assert [decoded[ref].borrow_cap for ref in (LIST27, LIST30, LIST32)] == [
        None,
        None,
        None,
    ]
    # list32 carries Just values at p[30]/p[31]: stake-credential options, not caps.
    list32_raw = decoded[LIST32].raw
    assert list32_raw[30].tag == list32_raw[31].tag == 121
    assert decoded[LIST34].borrow_cap == (4, 5)
    assert decoded[LIST34].supply_cap is None
    # list36 USDrf: no borrow cap (Nothing) but a supply cap.
    assert decoded[LIST36].borrow_cap is None
    assert decoded[LIST36].supply_cap == 10_000_000_000_000
    assert decoded[LIST34].withdraw_zero is None
    assert decoded[LIST34].loan_validator is None


def test_market_params_fields_0_to_26_share_one_shape_across_layouts():
    shapes = {
        ref: _shape(_fields(_datum("market_params", ref))[:27]) for ref in LAYOUTS
    }
    # p[26] is a list whose length varies per market; compare it as "a list".
    flattened = {
        ref: shape[:26] + ["list" if isinstance(shape[26], list) else shape[26]]
        for ref, shape in shapes.items()
    }
    assert len({repr(shape) for shape in flattened.values()}) == 1


@pytest.mark.parametrize("length", [27, 30, 32, 34])
def test_market_params_prefix_of_list36_decodes_the_shared_fields_alike(length):
    full_fields = _fields(_datum("market_params", LIST36))
    full = decode_market_params(_encode(full_fields))
    prefix = decode_market_params(_encode(full_fields[:length]))
    assert prefix.length == length
    for name in (
        "initial_qtoken_rate",
        "income_ratio",
        "legacy_loan_validator",
        "interest_period_ms",
        "rate_model",
        "max_ltv",
        "max_collaterals",
        "min_interest_rate",
        "close_factor",
        "liquidation_threshold",
        "action_validator",
        "batch_validator",
    ):
        assert getattr(prefix, name) == getattr(full, name), name
    assert prefix.withdraw_zero is None
    assert prefix.loan_validator is None
    if length < 34:
        assert prefix.borrow_cap is None
        assert prefix.supply_cap is None
    else:
        assert (prefix.borrow_cap, prefix.supply_cap) == (
            full.borrow_cap,
            full.supply_cap,
        )


@pytest.mark.parametrize(
    ("kind", "out_ref", "decode"),
    [
        *[("market_params", ref, decode_market_params) for ref in sorted(LAYOUTS)],
        ("market_state", GENESIS_ADA_STATE, decode_market_state),
        ("collateral_params", GENESIS_ADA_COLLATERAL, decode_collateral_params),
        ("collateral_params", SCRIPT_CLASS_COLLATERAL, decode_collateral_params),
    ],
)
def test_definite_and_indefinite_encodings_decode_alike(kind, out_ref, decode):
    datum = _datum(kind, out_ref)
    definite, indefinite = _definite(datum), _indefinite(datum)
    assert definite != indefinite
    assert decode(definite) == decode(datum) == decode(indefinite)


@pytest.mark.parametrize(
    ("datum", "decode"),
    [
        (FIRST_ADA_LOAN, decode_loan),
        (FIRST_ADA_FEED, decode_oracle_feed),
        (AGIX_FEED, decode_oracle_feed),
        (DJED_PEG_LIST5, decode_oracle_feed),
        (USDA_PEG_LIST6, decode_oracle_feed),
    ],
)
def test_inline_records_decode_alike_in_both_encodings(datum, decode):
    assert decode(_definite(datum)) == decode(datum) == decode(_indefinite(datum))


@pytest.mark.parametrize("length", [0, 26, 28, 29, 31, 33, 35, 37])
def test_market_params_unknown_lengths_raise(length):
    fields = _fields(_datum("market_params", LIST36))
    fields = (fields + [0])[:length]
    with pytest.raises(ValueError, match="unknown layout"):
        decode_market_params(_encode(fields))


def test_parked_market_params_is_not_a_record():
    # The parked output carries the params NFT on an empty constructor.
    assert _datum("market_params", PARKED_PARAMS) == "d87980"
    with pytest.raises(ValueError, match="not a field list"):
        decode_market_params("d87980")


@pytest.mark.parametrize(
    ("position", "value"),
    [
        (12, b"\x01\x02"),  # bytes where a rational belongs
        (12, [1, 0]),  # zero denominator
        (10, True),  # a bool is not an integer
        (9, 5),  # an integer where a script hash belongs
        (11, [[1, 2], [3, 4]]),  # a rate model of two parts
        (2, [10, 9, 0]),  # an income ratio of three parts
        (32, cbor2.CBORTag(123, [])),  # neither Just nor Nothing
        (32, [[4, 5]]),  # a bare list where the Maybe constructor belongs
        (33, cbor2.CBORTag(121, [b"\x00"])),  # a supply cap that is not an integer
        (34, [cbor2.CBORTag(121, [b"\x00" * 28])] * 2),  # pub-key credentials
        (35, cbor2.CBORTag(121, [])),  # a constructor where a script hash belongs
    ],
)
def test_market_params_wrong_field_types_raise(position, value):
    fields = _fields(_datum("market_params", LIST36))
    fields[position] = value
    with pytest.raises(ValueError, match="liqwid market params"):
        decode_market_params(_encode(fields))


@pytest.mark.parametrize(
    ("decode", "record"),
    list(DECODERS.items()),
    ids=list(DECODERS.values()),
)
@pytest.mark.parametrize(
    "datum",
    [
        "",  # empty
        "zz",  # not hex
        "9f0001",  # truncated indefinite array
        "8a00",  # truncated definite array
        "80",  # an empty list
        "d87980",  # an empty constructor
        "01",  # an integer
        "581c" + "00" * 28,  # a bytestring
        INFINITE_ALTERNATIVE,  # a general constructor with an infinite alternative
        DEEPLY_NESTED,  # arrays nested past the recursion limit
    ],
    ids=lambda datum: datum[:24],
)
def test_every_decoder_raises_value_error_on_malformed_input(decode, record, datum):
    with pytest.raises(ValueError, match=f"^liqwid {record}: ") as excinfo:
        decode(datum)
    assert str(excinfo.value).count("liqwid") == 1


@pytest.mark.parametrize(
    ("kind", "decode", "record"),
    [
        ("market_state", decode_market_state, "market state"),
        ("market_params", decode_market_params, "market params"),
        ("collateral_params", decode_collateral_params, "collateral params"),
        ("loan", decode_loan, "loan"),
        ("oracle", decode_oracle_feed, "oracle feed"),
    ],
)
def test_an_infinite_constructor_alternative_in_any_field_raises_value_error(
    kind,
    decode,
    record,
):
    fields = _fields(_genuine(kind)[0]["datum_cbor"])
    for position in range(len(fields)):
        variant = list(fields)
        variant[position] = INFINITE_TAG
        try:
            decode(_encode(variant))
        except ValueError as exc:
            assert str(exc).startswith(f"liqwid {record}: ")
            assert str(exc).count("liqwid") == 1


def test_an_unknown_constructor_tag_is_reported_with_the_record_prefix():
    fields = _fields(_datum("market_params", LIST36))
    fields[32] = cbor2.CBORTag(200, [])  # no Plutus constructor uses tag 200
    with pytest.raises(
        ValueError,
        match="^liqwid market params: not a constructor tag: 200$",
    ):
        decode_market_params(_encode(fields))


def test_every_decoder_accepts_bytes_as_well_as_hex():
    assert decode_loan(bytes.fromhex(FIRST_ADA_LOAN)) == decode_loan(FIRST_ADA_LOAN)
    assert decode_oracle_feed(bytes.fromhex(AGIX_FEED)) == decode_oracle_feed(
        AGIX_FEED,
    )


def test_market_state_genesis_ada_values():
    assert decode_market_state(
        _datum("market_state", GENESIS_ADA_STATE),
    ) == MarketStateDatum(
        cash=0,
        reserve=0,
        qtokens=0,
        principal=0,
        interest=0,
        index=16_777_216_000_000_000,
        rate=(2827979481, 2251799813685248),
        last_interest_ms=1_675_302_300_000,
        last_batch_ms=1_675_302_300_000,
        qtoken_rate=(1, 50),
        min_ada=3_000_000,
    )


@pytest.mark.parametrize("out_ref", [ADA_NFT_LIST0, ADA_NFT_LIST27])
def test_non_state_outputs_holding_the_ada_state_nft_raise(out_ref):
    with pytest.raises(ValueError, match="unknown layout"):
        decode_market_state(_datum("market_state", out_ref))


def test_every_captured_market_state_record_decodes():
    states = _genuine("market_state")
    assert states
    for rec in states:
        state = decode_market_state(rec["datum_cbor"])
        assert state.qtoken_rate[1] > 0, rec["out_ref"]


def test_collateral_params_exact_qtoken_genesis_ada():
    assert decode_collateral_params(
        _datum("collateral_params", GENESIS_ADA_COLLATERAL),
    ) == CollateralParamsDatum(
        collateral=ADA_QTOKEN,
        policy_wide=False,
        weight=(1, 1),
        liquidation_discount=(9, 10),
        treasury_split=(0, 1),
        underlying="lovelace",
        collateral_market_state=ADA_MARKET_STATE_NFT,
    )


def test_collateral_params_script_class_is_policy_wide():
    assert decode_collateral_params(
        _datum("collateral_params", SCRIPT_CLASS_COLLATERAL),
    ) == CollateralParamsDatum(
        collateral=POLICY_WIDE_POLICY,
        policy_wide=True,
        weight=(81, 100_000_000),
        liquidation_discount=(93, 100),
        treasury_split=(0, 1),
        underlying="",
        collateral_market_state="",
    )


def test_every_captured_collateral_params_record_decodes():
    records = _genuine("collateral_params")
    assert records
    for rec in records:
        params = decode_collateral_params(rec["datum_cbor"])
        assert len(params.collateral) == 56, rec["out_ref"]
        assert params.collateral != "lovelace"
        # Only a script-class record lacks the collateral-market link.
        assert (params.underlying == "") == params.policy_wide, rec["out_ref"]


def test_collateral_params_wrong_field_types_raise():
    fields = _fields(_datum("collateral_params", GENESIS_ADA_COLLATERAL))
    for position, value in (
        (0, [ADA_QTOKEN]),  # a bare list where the asset class belongs
        (0, cbor2.CBORTag(122, [b"\x01"])),  # Constr1 without the [policy, name] pair
        (0, cbor2.CBORTag(121, [b""])),  # policy-wide with an empty policy
        (1, 7),  # an integer where the weight belongs
        (4, cbor2.CBORTag(121, [[1, 2]])),  # a link of integers
    ):
        variant = list(fields)
        variant[position] = value
        with pytest.raises(ValueError, match="liqwid collateral params"):
            decode_collateral_params(_encode(variant))


def test_loan_first_ada_loan_values():
    assert decode_loan(FIRST_ADA_LOAN) == LoanDatum(
        owner="23c820c49b92eb4b6cce4aded5baddb9efe5b884fcc93a1e5834b1c4",
        principal=101_000_000,
        interest=0,
        min_interest=80_800,
        index=16_777_216_000_000_000,
    )


def test_every_captured_loan_record_decodes_in_both_encodings():
    loans = _genuine("loan")
    assert loans
    for rec in loans:
        loan = decode_loan(rec["datum_cbor"])
        assert len(loan.owner) == 56, rec["out_ref"]
        assert loan.principal >= 0
    # Legacy loans are indefinite lists (0x9f); current ones also use 0x85.
    assert {rec["datum_cbor"][:2] for rec in loans} == {"9f", "85"}


def test_parked_and_datumless_loan_outputs_raise():
    non_records = _non_records("loan")
    parked = [rec for rec in non_records if rec["datum_cbor"]]
    assert parked
    assert len(parked) < len(non_records)  # a datum-less output is captured too
    for rec in non_records:
        match = "not a field list" if rec["datum_cbor"] else "^liqwid loan: "
        with pytest.raises(ValueError, match=match):
            decode_loan(rec["datum_cbor"])


def test_loan_wrong_field_types_raise():
    fields = _fields(FIRST_ADA_LOAN)
    for position, value in ((0, 5), (1, b"\x01"), (4, [1, 2]), (2, False)):
        variant = list(fields)
        variant[position] = value
        with pytest.raises(ValueError, match="liqwid loan"):
            decode_loan(_encode(variant))


def test_oracle_feed_list5_ada():
    assert decode_oracle_feed(FIRST_ADA_FEED) == OracleFeedDatum(
        updater="cfe224295a282d69edda5fa8de4f131e2b9cd21a6c9235597fa4ff6b",
        asset="lovelace",
        policy_wide=False,
        price=(189, 500),
        ts_ms=1_675_298_628_902,
        validity_ms=60_000,
        caps=None,
    )


def test_oracle_feed_list6_keeps_the_caps_field():
    feed = decode_oracle_feed(AGIX_FEED)
    assert feed == OracleFeedDatum(
        updater="2f97c124ebe90d23fac1be4111b5fda977021cb3f88cbe5d87145b61",
        asset="f43a62fdc3965df486de8a0d32fe800963589c41b38946602a0dc53541474958",
        policy_wide=False,
        price=(72281, 10_000_000),
        ts_ms=1_727_280_094_905,
        validity_ms=900_000,
        caps=None,
    )
    assert feed.caps == cbor2.CBORTag(121, [[[1, 1000], [1, 200]]])


@pytest.mark.parametrize(
    ("datum", "asset"),
    [
        (
            DJED_PEG_LIST5,
            "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
            "446a65644d6963726f555344",
        ),
        (
            USDA_PEG_LIST6,
            "fe7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae45655534441",
        ),
    ],
)
def test_oracle_peg_feeds(datum, asset):
    feed = decode_oracle_feed(datum)
    assert (feed.asset, feed.policy_wide, feed.price) == (asset, False, (1, 1))


def _policy_wide_feed() -> str:
    """A Constr0[policy] feed shaped like the retired one at the hard-caps validator."""
    return cbor2.dumps(
        [
            bytes.fromhex(UPDATER),
            cbor2.CBORTag(121, [bytes.fromhex(POLICY_WIDE_POLICY)]),
            [10000, 1],
            1_743_433_521_000,
            900_000,
            cbor2.CBORTag(122, []),
        ],
    ).hex()


def test_policy_wide_oracle_feed_is_its_policy_never_lovelace():
    feed = decode_oracle_feed(_policy_wide_feed())
    assert feed.policy_wide is True
    assert feed.asset == POLICY_WIDE_POLICY
    assert feed.price == (10000, 1)
    # The exact-asset reader keeps its old answer for this shape (see its docstring).
    assert parse_liqwid_oracle_v2(_policy_wide_feed()) == ("lovelace", (10000, 1))


def test_empty_asset_class_is_lovelace():
    fields = _fields(_policy_wide_feed())
    fields[1] = cbor2.CBORTag(122, [[b"", b""]])
    feed = decode_oracle_feed(_encode(fields))
    assert (feed.asset, feed.policy_wide) == ("lovelace", False)


def test_every_captured_oracle_feed_decodes():
    feeds = [decode_oracle_feed(rec["datum_cbor"]) for rec in _genuine("oracle")]
    assert {feed.caps is None for feed in feeds} == {True, False}  # list5 and list6
    policy_wide = [feed for feed in feeds if feed.policy_wide]
    assert policy_wide
    for feed in policy_wide:
        assert len(feed.asset) == 56
        assert feed.asset != "lovelace"


def test_oracle_feed_wrong_shapes_raise():
    fields = _fields(FIRST_ADA_FEED)
    for position, value in (
        (1, cbor2.CBORTag(123, [b""])),  # Constr2 is no asset class
        (2, [189, 0]),  # zero denominator
        (3, b"\x01"),  # bytes where the timestamp belongs
    ):
        variant = list(fields)
        variant[position] = value
        with pytest.raises(ValueError, match="liqwid oracle feed"):
            decode_oracle_feed(_encode(variant))
    with pytest.raises(ValueError, match="unknown layout"):
        decode_oracle_feed(_encode([*fields, 0, 0]))


def _action_datum(deltas: list, nonce: object) -> str:
    return cbor2.dumps([deltas, nonce]).hex()


def test_decode_action_reads_the_signed_deltas():
    datum = _action_datum(
        [5_000_000, -250_000_000, 1_000_000, 0, 800],
        cbor2.CBORTag(121, [b"\x07"]),
    )
    action = decode_action(datum)
    assert action == ActionDatum(
        supply_delta=5_000_000,
        qtoken_delta=-250_000_000,
        principal_delta=1_000_000,
        interest_delta=0,
        min_interest_paid=800,
        nonce=None,
    )
    assert action.nonce == cbor2.CBORTag(121, [b"\x07"])
    assert decode_action(_indefinite(datum)) == action


def test_decode_action_wrong_shapes_raise():
    with pytest.raises(ValueError, match="liqwid action"):
        decode_action(_action_datum([1, 2, 3, 4], 0))
    with pytest.raises(ValueError, match="liqwid action"):
        decode_action(_action_datum([1, 2, 3, 4, b"\x05"], 0))
    with pytest.raises(ValueError, match="unknown layout"):
        decode_action(cbor2.dumps([[1, 2, 3, 4, 5]]).hex())


def test_records_are_frozen_and_hash_without_their_raw_fields():
    params = decode_market_params(_datum("market_params", LIST36))
    feed = decode_oracle_feed(AGIX_FEED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.length = 0  # type: ignore[misc]
    assert hash(params) == hash(dataclasses.replace(params, raw=()))
    assert hash(feed) == hash(dataclasses.replace(feed, caps=None))


def test_existing_readers_agree_with_the_record_decoders_on_captured_records():
    # The five original readers keep their behaviour; on every captured genuine
    # record they return the fields the record decoders read.
    for ref in LAYOUTS:
        datum = _datum("market_params", ref)
        params = decode_market_params(datum)
        assert parse_liqwid_market_param(datum) == (
            params.initial_qtoken_rate,
            params.max_ltv,
        )
    genesis = _datum("market_state", GENESIS_ADA_STATE)
    state = decode_market_state(genesis)
    assert parse_liqwid_market_state(genesis) == state.qtoken_rate
    for datum in (FIRST_ADA_FEED, AGIX_FEED, DJED_PEG_LIST5, USDA_PEG_LIST6):
        feed = decode_oracle_feed(datum)
        assert parse_liqwid_oracle_v2(datum) == (feed.asset, feed.price)
        assert liqwid_oracle_feed_id(datum) == feed.updater
    assert liqwid_qtoken_rate(qtoken_rate_num=1, qtoken_rate_denom=50) == (1, 50)


def test_existing_readers_keep_their_outputs_on_mainnet_datums():
    assert parse_liqwid_oracle_v2(FIX["oracle_v2"]["datum"]) == (
        "lovelace",
        (16271, 100000),
    )
    assert parse_liqwid_market_param(FIX["market_param"]["datum"]) == (
        (1, 50),
        (8696, 10000),
    )
    assert parse_liqwid_market_state(FIX["market_state"]["datum"]) == (
        360073866973321,
        12033383669199582,
    )
    assert liqwid_oracle_feed_id(_policy_wide_feed()) == UPDATER
    # The record decoders read the same values from these datums.
    assert decode_oracle_feed(FIX["oracle_v2"]["datum"]).price == (16271, 100000)
    params = decode_market_params(FIX["market_param"]["datum"])
    assert (params.initial_qtoken_rate, params.max_ltv) == ((1, 50), (8696, 10000))
    assert decode_market_state(FIX["market_state"]["datum"]).qtoken_rate == (
        360073866973321,
        12033383669199582,
    )


# --- One decode path: field_count and LIQWID_DECODE_ERRORS -------------------

OVERFLOW_TAGS = [
    "c4821bffffffffffffff0101",  # decimal fraction, exponent overflows
    "c5821bffffffffffffff0101",  # bigfloat, exponent overflows
]


def test_liqwid_decode_errors_cover_every_decode_failure():
    for error in (*DECODE_ERRORS, cbor2.CBORError, EOFError, RecursionError):
        assert issubclass(error, LIQWID_DECODE_ERRORS), error
    assert issubclass(ArithmeticError, LIQWID_DECODE_ERRORS)
    assert issubclass(decimal.Overflow, LIQWID_DECODE_ERRORS)


@pytest.mark.parametrize(
    ("decode", "record"),
    list(DECODERS.items()),
    ids=list(DECODERS.values()),
)
@pytest.mark.parametrize("datum", OVERFLOW_TAGS)
def test_every_decoder_rejects_an_overflowing_number_tag(decode, record, datum):
    with pytest.raises(ValueError, match=f"^liqwid {record}: "):
        decode(datum)


@pytest.mark.parametrize(
    ("decode", "record"),
    list(DECODERS.items()),
    ids=list(DECODERS.values()),
)
def test_an_arithmetic_error_while_decoding_is_a_value_error(
    monkeypatch,
    decode,
    record,
):
    """The rejection does not depend on how the cbor2 build reports an overflow."""

    def overflow(_raw: bytes) -> object:
        raise decimal.Overflow

    monkeypatch.setattr(cbor2, "loads", overflow)
    with pytest.raises(ValueError, match=f"^liqwid {record}: malformed datum$"):
        decode("80")


@pytest.mark.parametrize("kind", ["market_state", "market_params", "loan", "oracle"])
def test_field_count_of_a_record_in_both_encodings(kind):
    for rec in _genuine(kind):
        length = len(_fields(rec["datum_cbor"]))
        assert field_count(rec["datum_cbor"]) == length
        assert field_count(_definite(rec["datum_cbor"])) == length
        assert field_count(_indefinite(rec["datum_cbor"])) == length
        assert field_count(bytes.fromhex(rec["datum_cbor"])) == length


@pytest.mark.parametrize("length", [0, 1, 26, 37])
def test_field_count_reads_lengths_outside_the_layouts(length):
    assert field_count(_encode(list(range(length)))) == length


@pytest.mark.parametrize("parked", ["d87980", "d8799fff"])
def test_field_count_of_a_parked_constructor_is_none(parked):
    assert field_count(parked) is None
    assert field_count(bytes.fromhex(parked)) is None


@pytest.mark.parametrize(
    "datum",
    [
        "",  # no inline datum
        "zz",  # not hex
        "9f0001",  # truncated indefinite array
        "d87a80",  # an empty Constr1
        "d8798100",  # a Constr0 that is not empty
        "01",  # an integer
        "581c" + "00" * 28,  # a bytestring
        DEEPLY_NESTED,  # arrays nested past the recursion limit
        *OVERFLOW_TAGS,
    ],
    ids=lambda datum: datum[:24],
)
def test_field_count_raises_value_error_on_anything_else(datum):
    with pytest.raises(ValueError, match="^liqwid record: "):
        field_count(datum)
