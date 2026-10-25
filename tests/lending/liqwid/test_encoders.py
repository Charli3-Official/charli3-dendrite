"""Liqwid datum encoders, datum style and the named MarketParams fields.

Every genuine captured MarketState and loan record re-encodes to its exact on-chain
bytes in the style it was written in.
"""

from collections import Counter

import cbor2
import pytest

from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.datums import decode_market_params
from charli3_dendrite.lending.liqwid.datums import decode_market_state
from charli3_dendrite.lending.liqwid.datums import encode_action_datum
from charli3_dendrite.lending.liqwid.datums import encode_loan_datum
from charli3_dendrite.lending.liqwid.datums import encode_market_state
from tests.lending.liqwid.records import records_of

ADA_MARKET_ID = "5a3cb4f52fb3a00ab5abe62080618623811ccafab9c760d0b686e44c"
OWNER = "23c820c49b92eb4b6cce4aded5baddb9efe5b884fcc93a1e5834b1c4"


def _genuine(kind: str) -> list[dict]:
    return [rec for rec in records_of(kind) if rec["label"] is None]


def _ids(records: list[dict]) -> list[str]:
    return [rec["out_ref"] for rec in records]


MARKET_STATES = _genuine("market_state")
LOANS = _genuine("loan")


def test_the_captured_record_counts():
    assert len(MARKET_STATES) == 39
    assert len(LOANS) == 824


@pytest.mark.parametrize("rec", MARKET_STATES, ids=_ids(MARKET_STATES))
def test_market_state_round_trips_to_its_bytes(rec):
    datum = rec["datum_cbor"]
    indefinite = datum_style(datum) == "indefinite"
    encoded = encode_market_state(decode_market_state(datum), indefinite=indefinite)
    assert encoded == bytes.fromhex(datum)


@pytest.mark.parametrize("rec", LOANS, ids=_ids(LOANS))
def test_loan_round_trips_to_its_bytes(rec):
    datum = rec["datum_cbor"]
    indefinite = datum_style(datum) == "indefinite"
    encoded = encode_loan_datum(decode_loan(datum), indefinite=indefinite)
    assert encoded == bytes.fromhex(datum)


def test_datum_style_counts_of_the_captured_records():
    assert Counter(datum_style(rec["datum_cbor"]) for rec in MARKET_STATES) == {
        "definite": 35,
        "indefinite": 4,
    }
    assert Counter(datum_style(rec["datum_cbor"]) for rec in LOANS) == {
        "definite": 780,
        "indefinite": 44,
    }


def test_datum_style_reads_hex_and_bytes_alike():
    assert datum_style("9fff") == "indefinite"
    assert datum_style("9FFF") == "indefinite"
    assert datum_style(b"\x9f\xff") == "indefinite"
    assert datum_style("80") == "definite"
    assert datum_style(b"\x80") == "definite"


def test_datum_style_of_an_empty_datum_raises():
    with pytest.raises(ValueError, match="liqwid record: "):
        datum_style("")
    with pytest.raises(ValueError, match="liqwid record: "):
        datum_style(b"")


def test_encoders_default_to_definite():
    loan = LoanDatum(
        owner=OWNER,
        principal=1_000_000,
        interest=2,
        min_interest=3,
        index=1_000_000_000_000,
    )
    encoded = encode_loan_datum(loan)
    assert encoded[0] == 0x85
    assert datum_style(encoded) == "definite"
    assert decode_loan(encoded) == loan
    indefinite = encode_loan_datum(loan, indefinite=True)
    assert datum_style(indefinite) == "indefinite"
    assert indefinite.endswith(b"\xff")
    assert decode_loan(indefinite) == loan


def test_market_state_indefinite_walks_the_nested_rationals():
    state = decode_market_state(MARKET_STATES[0]["datum_cbor"])
    encoded = encode_market_state(state, indefinite=True)

    def pair(rational: tuple[int, int]) -> bytes:
        return b"\x9f" + cbor2.dumps(rational[0]) + cbor2.dumps(rational[1]) + b"\xff"

    # The outer list and both rational pairs are indefinite.
    expected = (
        b"\x9f"
        + b"".join(
            cbor2.dumps(value)
            for value in (
                state.cash,
                state.reserve,
                state.qtokens,
                state.principal,
                state.interest,
                state.index,
            )
        )
        + pair(state.rate)
        + cbor2.dumps(state.last_interest_ms)
        + cbor2.dumps(state.last_batch_ms)
        + pair(state.qtoken_rate)
        + cbor2.dumps(state.min_ada)
        + b"\xff"
    )
    assert encoded == expected
    assert decode_market_state(encoded) == state


def test_market_state_big_integers_encode_as_cbor_bignums():
    state = decode_market_state(MARKET_STATES[0]["datum_cbor"])
    big = 2**64 + 5
    wide = type(state)(**{**state.__dict__, "index": big})
    encoded = encode_market_state(wide)
    assert cbor2.loads(encoded)[5] == big
    assert cbor2.dumps(big) in encoded
    assert decode_market_state(encoded) == wide


@pytest.mark.parametrize(
    "nonce",
    [7, 2**70, b"\x01\x02", cbor2.CBORTag(121, [b"\x07"]), [1, [2, 3]]],
)
@pytest.mark.parametrize("indefinite", [False, True])
def test_action_encoder_round_trips(nonce, indefinite):
    action = ActionDatum(
        supply_delta=5_000_000,
        qtoken_delta=-250_000_000,
        principal_delta=1_000_000,
        interest_delta=0,
        min_interest_paid=800,
        nonce=nonce,
    )
    encoded = encode_action_datum(action, indefinite=indefinite)
    style = "indefinite" if indefinite else "definite"
    assert datum_style(encoded) == style
    decoded = decode_action(encoded)
    assert decoded == action
    assert decoded.nonce == nonce
    if not indefinite:
        assert encoded == cbor2.dumps(
            [[5_000_000, -250_000_000, 1_000_000, 0, 800], nonce],
        )
    else:
        assert encoded.startswith(b"\x9f\x9f")


def test_action_indefinite_encoding_walks_constructor_fields():
    action = ActionDatum(
        supply_delta=1,
        qtoken_delta=2,
        principal_delta=3,
        interest_delta=4,
        min_interest_paid=5,
        nonce=cbor2.CBORTag(121, [b"\x07"]),
    )
    encoded = encode_action_datum(action, indefinite=True)
    assert encoded == bytes.fromhex("9f9f0102030405ffd8799f4107ffff")


def _live_ada_params() -> MarketParamsDatum:
    live = [
        rec
        for rec in _genuine("market_params")
        if rec["live"] and rec["market_id"] == ADA_MARKET_ID
    ]
    assert len(live) == 1
    return decode_market_params(live[0]["datum_cbor"])


def test_market_params_named_fields_on_the_live_ada_params():
    params = _live_ada_params()
    assert params.min_value == 81_000_000
    assert params.max_batch_time_ms == 15_000_000
    assert params.action_count == 4


def _keyword_params(raw: tuple[object, ...]) -> MarketParamsDatum:
    return MarketParamsDatum(
        length=36,
        initial_qtoken_rate=(1, 50),
        income_ratio=(10, 9, 0, 0),
        legacy_loan_validator=None,
        interest_period_ms=2_000_000,
        rate_model=(),
        max_ltv=(3, 4),
        max_collaterals=5,
        min_interest_rate=(1, 1250),
        close_factor=(1, 1),
        liquidation_threshold=(1, 1),
        action_validator=None,
        batch_validator=None,
        borrow_cap=None,
        supply_cap=None,
        withdraw_zero=None,
        loan_validator=None,
        raw=raw,
    )


@pytest.mark.parametrize("name", ["min_value", "max_batch_time_ms", "action_count"])
def test_market_params_named_fields_raise_without_raw(name):
    params = _keyword_params(())
    with pytest.raises(ValueError, match="liqwid market params: "):
        getattr(params, name)


def test_market_params_action_count_raises_on_a_list():
    params = _keyword_params((0, 81_000_000, 0, 15_000_000, 0, 0, 0, [4]))
    assert params.min_value == 81_000_000
    assert params.max_batch_time_ms == 15_000_000
    with pytest.raises(ValueError, match="liqwid market params: "):
        _ = params.action_count


def test_market_params_named_fields_refuse_a_bool():
    params = _keyword_params((0, True, 0, 15_000_000, 0, 0, 0, 4))
    with pytest.raises(ValueError, match="liqwid market params: "):
        _ = params.min_value
