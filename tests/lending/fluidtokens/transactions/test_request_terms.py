"""Unit: RequestTerms round-trips a captured RequestDatum byte-for-byte."""

from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_synth_request_datum_round_trips_capture() -> None:
    from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CreateRequestSnapshot,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
        RequestTerms,
        synth_request_datum,
    )

    fix = json.loads((_FIX / "create_request.json").read_text())
    cap = CreateRequestSnapshot.from_capture(fix)
    datum = RequestDatum.from_cbor(bytes.fromhex(cap.request_datum))
    terms = RequestTerms.from_request_datum(datum)
    assert synth_request_datum(terms).to_cbor().hex() == cap.request_datum
