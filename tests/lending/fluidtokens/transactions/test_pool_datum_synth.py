"""PoolTerms + synth_pool_datum reproduce a real on-chain PoolDatum byte-exact."""

from __future__ import annotations

import json
from pathlib import Path

from charli3_dendrite.lending.fluidtokens.constants import POOL_POLICY
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import PoolTerms
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    synth_pool_datum,
)

_FIX = Path(__file__).parent / "fixtures"


def _captured_pool_datum_hex() -> str:
    fix = json.loads((_FIX / "pool_create.json").read_text())
    out = next(
        u
        for u in fix["outputs"]
        if u.get("datum") and any(a[0] == POOL_POLICY for a in u["assets"])
    )
    return out["datum"]


def test_synth_pool_datum_round_trips_capture() -> None:
    """PoolTerms recovered from a real PoolDatum re-synthesize the exact CBOR."""
    datum_hex = _captured_pool_datum_hex()
    decoded = PoolDatum.from_cbor(bytes.fromhex(datum_hex))
    terms = PoolTerms.from_pool_datum(decoded)
    rebuilt = synth_pool_datum(terms)
    assert rebuilt.to_cbor().hex() == datum_hex
