"""Gated: dbsync resolvers reconstruct the captured pool's UTxOs from chain state."""

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
def test_resolve_utxo_by_outref_matches_capture() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CancelPoolSnapshot,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
        resolve_utxo_by_outref,
    )

    fix = json.loads((_FIX / "pool_cancel.json").read_text())
    cap = CancelPoolSnapshot.from_capture(fix)
    backend = get_backend()
    resolved = resolve_utxo_by_outref(backend, *cap.pool.out_ref, allow_spent=True)
    assert resolved == cap.pool


@_gate()
def test_cancel_from_backend_resolves_capture_fields() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CancelPoolSnapshot,
    )

    fix = json.loads((_FIX / "pool_cancel.json").read_text())
    cap = CancelPoolSnapshot.from_capture(fix)
    backend = get_backend()
    snap = CancelPoolSnapshot.from_backend(
        backend,
        pool_utxo=cap.pool.out_ref,
        allow_spent_pool=True,
        config_outref=cap.config.out_ref,
        pool_spend_ref_outref=cap.pool_spend_script_ref.out_ref,
        pool_policy_ref_outref=cap.pool_policy_script_ref.out_ref,
    )
    assert snap.pool == cap.pool
    assert snap.config == cap.config
    assert snap.pool_spend_script_ref == cap.pool_spend_script_ref
    assert snap.pool_policy_script_ref == cap.pool_policy_script_ref
    assert snap.pool_id == cap.pool_id
    assert snap.lender_pkh == cap.lender_pkh
