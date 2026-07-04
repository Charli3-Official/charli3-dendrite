"""Offline: CreateRequestSnapshot.from_backend rebuilds the captured create-request."""
from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_create_request_from_backend_matches_capture(monkeypatch) -> None:
    from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CreateRequestSnapshot,
        _as_utxo,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
        RequestTerms,
    )

    fix = json.loads((_FIX / "create_request.json").read_text())
    cap = CreateRequestSnapshot.from_capture(fix)
    terms = RequestTerms.from_request_datum(
        RequestDatum.from_cbor(bytes.fromhex(cap.request_datum)),
    )
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

    snap = CreateRequestSnapshot.from_backend(
        None,
        terms=terms,
        borrower_address="",
        request_lovelace=cap.request_lovelace,
        collateral=cap.collateral,
        request_address=cap.request_address,
        funding_outrefs=[u.out_ref for u in cap.funding],
        config_outref=cap.config.out_ref,
        request_policy_ref_outref=cap.request_policy_script_ref.out_ref,
        input_ref=cap.input_ref,
    )
    assert snap.funding == cap.funding
    assert snap.config == cap.config
    assert snap.request_policy_script_ref == cap.request_policy_script_ref
    assert (
        snap.request_datum == cap.request_datum
    )  # synth round-trips to on-chain datum
    assert snap.request_address == cap.request_address
    assert snap.request_lovelace == cap.request_lovelace
    assert snap.collateral == cap.collateral
    assert snap.input_ref == cap.input_ref
