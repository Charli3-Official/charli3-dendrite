"""Unit: _resolve_recast_fee reproduces the captured recast fee output."""
from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_resolve_recast_fee_matches_captured_fee_output() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        RecastSnapshot,
        _PROTOCOL_FEE_ADDRESS,
        _resolve_recast_fee,
    )

    fix = json.loads((_FIX / "recast.json").read_text())
    cap = RecastSnapshot.from_capture(fix)

    fee_output = next(
        o for o in fix["outputs"] if o["address"] == _PROTOCOL_FEE_ADDRESS
    )

    fee_address, fee_lovelace = _resolve_recast_fee(cap.config)
    assert (fee_address, fee_lovelace) == (_PROTOCOL_FEE_ADDRESS, 9_000_000)
    assert (fee_address, fee_lovelace) == (
        fee_output["address"],
        fee_output["lovelace"],
    )
    assert (fee_address, fee_lovelace) == (cap.fee_address, cap.fee_lovelace)
