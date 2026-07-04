"""Gated: dbsync resolves a CancelRequestSnapshot matching the capture; Ogmios evaluates."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

_FIX = Path(__file__).parent / "fixtures"


def _gate() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not os.environ.get("DBSYNC_HOST"),
        reason="needs dbsync",
    )


@_gate()
def test_cancel_request_from_backend_resolves_capture_fields() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CancelRequestSnapshot,
    )

    fix = json.loads((_FIX / "cancel_request.json").read_text())
    cap = CancelRequestSnapshot.from_capture(fix)
    backend = get_backend()
    snap = CancelRequestSnapshot.from_backend(
        backend,
        request_utxo=cap.request.out_ref,
        allow_spent_request=True,
        # Pin funding to the captured inputs: this gives byte-exact funding
        # equality and fixes the burn redeemer's input_ref (from_backend sets
        # mint_input_ref = funding[0].out_ref) to a captured spent input.
        funding_outrefs=[u.out_ref for u in cap.funding],
        config_outref=cap.config.out_ref,
        request_spend_ref_outref=cap.request_spend_script_ref.out_ref,
        request_policy_ref_outref=cap.request_policy_script_ref.out_ref,
    )
    assert snap.request == cap.request
    assert snap.request_id == cap.request_id
    assert snap.borrower_pkh == cap.borrower_pkh
    assert snap.config == cap.config
    assert snap.funding == cap.funding
    assert snap.request_spend_script_ref == cap.request_spend_script_ref
    assert snap.request_policy_script_ref == cap.request_policy_script_ref
    assert snap.mint_input_ref == cap.mint_input_ref


@_gate()
def test_cancel_request_from_backend_builds_and_evaluates() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CancelRequestSnapshot,
        _as_utxo,
        ogmios_entry,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.request import (
        build_cancel_request,
    )
    from charli3_dendrite.lending.transactions.infra import (
        EvalContext,
        assemble_unsigned,
        evaluate_tx_cbor,
    )
    from pycardano import TransactionBuilder

    if not os.environ.get("OGMIOS_HOST"):
        pytest.skip("needs ogmios")

    fix = json.loads((_FIX / "cancel_request.json").read_text())
    cap = CancelRequestSnapshot.from_capture(fix)
    backend = get_backend()
    snap = CancelRequestSnapshot.from_backend(
        backend,
        request_utxo=cap.request.out_ref,
        allow_spent_request=True,
        # Pin funding to the captured inputs so the burn redeemer's input_ref
        # (from_backend sets mint_input_ref = funding[0].out_ref) points at a
        # captured spent input that is present in ``additional`` for Ogmios.
        funding_outrefs=[u.out_ref for u in cap.funding],
        config_outref=cap.config.out_ref,
        request_spend_ref_outref=cap.request_spend_script_ref.out_ref,
        request_policy_ref_outref=cap.request_policy_script_ref.out_ref,
    )
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    build_cancel_request(tx_builder, snapshot=snap)
    additional = [ogmios_entry(_as_utxo(u)) for u in fix["inputs"] + fix["ref_inputs"]]
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), additional)
    assert budgets
    # A request cancel drives three script executions: the request spend, the
    # request-NFT burn (mint), and the request-policy reward (Cancel) withdrawal.
    assert len(budgets) == 3
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
