"""Shared FluidTokens V4 fixture records and helpers for the V4 tests."""

import json
from pathlib import Path

from charli3_dendrite.lending.fluidtokens.datums import Asset
from tests.lending.records_common import record_info as record_info

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())

# Oracle tokens no feed can hold, so a side naming one names no oracle. A feed holds
# its oracle NFT, and an NFT's policy id is a 28-byte script hash.
NO_ORACLE_TOKENS = {
    "no-oracle-marker": Asset(b"NONE", b"NONE"),
    "empty-token": Asset(b"", b""),
    "empty-policy": Asset(b"", b"oracle"),
    "marker-policy": Asset(b"NONE", b"oracle"),
}


def v4_request_record() -> dict:
    """A V4 request fixture entry (the fixture has none): the V3 request, V4 terms."""
    import dataclasses

    from charli3_dendrite.lending.fluidtokens import datums as v3
    from charli3_dendrite.lending.fluidtokens_v4 import constants as c
    from charli3_dendrite.lending.fluidtokens_v4 import datums as v4
    from charli3_dendrite.lending.units import script_payment_address

    v3_fix = json.loads(
        (
            Path(__file__).parents[1] / "fluidtokens" / "fixtures" / "entities.json"
        ).read_text(),
    )
    v3_request = v3.RequestDatum.from_cbor(v3_fix["request"]["datum_cbor"])
    values = {
        f.name: getattr(v3_request, f.name) for f in dataclasses.fields(v3.RequestDatum)
    }
    values["common_data"] = v4.PoolDatum.from_cbor(
        FIX["pool"][0]["datum_cbor"],
    ).common_data
    request = v4.RequestDatum(**values)
    return {
        "out_ref": "ab" * 32 + "#0",
        "address": script_payment_address(c.REQUEST_SPEND_SKH),
        "datum_cbor": request.to_cbor_hex(),
        "assets": {"lovelace": 5_000_000, c.REQUEST_POLICY + "cd" * 28: 1},
    }
