"""Offline: CancelRequestSnapshot.from_backend rebuilds the captured cancel."""

from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_cancel_request_from_backend_matches_capture(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CancelRequestSnapshot,
        _as_utxo,
    )

    fix = json.loads((_FIX / "cancel_request.json").read_text())
    cap = CancelRequestSnapshot.from_capture(fix)
    by_ref = {
        tuple(u["out_ref"]): _as_utxo(u)
        for u in fix["inputs"] + fix["ref_inputs"]
        if u.get("out_ref")
    }
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_utxo_by_outref",
        lambda backend, h, i, *, allow_spent=False: by_ref[(h, i)],
    )
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_config_utxo",
        lambda backend, *, allow_spent=False: cap.config,
    )
    monkeypatch.setattr(
        "charli3_dendrite.lending.fluidtokens.transactions.resolve.resolve_funding",
        lambda backend, address, **kwargs: cap.funding,
    )

    snap = CancelRequestSnapshot.from_backend(
        None,
        request_utxo=cap.request.out_ref,
        borrower_address=cap.request.address,
        allow_spent_request=True,
        config_outref=cap.config.out_ref,
        request_spend_ref_outref=cap.request_spend_script_ref.out_ref,
        request_policy_ref_outref=cap.request_policy_script_ref.out_ref,
    )
    assert snap.request == cap.request
    assert snap.config == cap.config
    assert snap.funding == cap.funding
    assert snap.request_id == cap.request_id
    assert snap.borrower_pkh == cap.borrower_pkh
    assert snap.request_spend_script_ref == cap.request_spend_script_ref
    assert snap.request_policy_script_ref == cap.request_policy_script_ref
    # burn redeemer input_ref = the first funding input (matches the capture)
    assert snap.mint_input_ref == cap.mint_input_ref
