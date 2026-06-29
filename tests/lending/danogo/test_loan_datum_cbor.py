"""LoanDatum must round-trip byte-exact against the captured loan output datum."""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.datums import LoanDatum

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)


def _loan_output():
    for out in FIX["outputs"].values():
        if out["datum"] and any(
            a[0] == FIX["loan_skh"] and a[2] == "1" for a in out["assets"]
        ):
            return out
    raise AssertionError("no loan output in fixture")


def test_loan_datum_round_trips_byte_exact():
    raw = bytes.fromhex(_loan_output()["datum"])
    datum = LoanDatum.from_cbor(raw)
    assert datum.to_cbor() == raw


def test_loan_datum_fields_are_sane():
    datum = LoanDatum.from_cbor(bytes.fromhex(_loan_output()["datum"]))
    assert datum.loan_amount > 0
    assert datum.initial_interest_index > 0
    assert len(datum.token) == 2
