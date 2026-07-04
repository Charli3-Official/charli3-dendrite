"""Unit: _resolve_protocol_fee reproduces the captured repay fee output."""
from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_resolve_protocol_fee_matches_captured_fee_output() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        RepaySnapshot,
        _resolve_protocol_fee,
    )

    fix = json.loads((_FIX / "repay_full.json").read_text())
    cap = RepaySnapshot.from_capture(fix)
    fee_address, fee_lovelace = _resolve_protocol_fee(cap.config)
    assert fee_address == cap.fee_address
    assert fee_lovelace == cap.fee_lovelace
