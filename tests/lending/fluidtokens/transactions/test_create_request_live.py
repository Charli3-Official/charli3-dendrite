"""Gated: dbsync live-resolution + Ogmios e2e for ``CreateRequestSnapshot.from_backend``.

Two proofs that the live create-request resolver produces a faithful, buildable
snapshot:

* dbsync live resolution reconstructs every captured create-request field (funding,
  config + request-policy reference inputs, the synthesized ``RequestDatum``, request
  address / lovelace / collateral, and the spent ``input_ref``).
* A transaction forward-built from the LIVE-resolved snapshot evaluates on the real
  Ogmios, returning one positive budget: the request-NFT mint. Nothing is signed or
  submitted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

load_dotenv()
from charli3_dendrite.lending.transactions.infra import EvalContext  # noqa: E402
from charli3_dendrite.lending.transactions.infra import assemble_unsigned  # noqa: E402
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor  # noqa: E402
from pycardano import TransactionBuilder  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def _dbsync_gate() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not os.environ.get("DBSYNC_HOST"),
        reason="needs dbsync",
    )


def _dbsync_ogmios_gate(file: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not (
            os.environ.get("DBSYNC_HOST")
            and os.environ.get("OGMIOS_HOST")
            and (FIXTURES_DIR / file).exists()
        ),
        reason="needs dbsync + ogmios + the captured request fixture",
    )


def _utxo_entry(u: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {"ada": {"lovelace": int(u["lovelace"])}}
    for policy, asset_name, qty in u["assets"]:
        value.setdefault(policy, {})[asset_name] = int(qty)
    entry: dict[str, Any] = {
        "transaction": {"id": u["out_ref"][0]},
        "index": u["out_ref"][1],
        "address": u["address"],
        "value": value,
    }
    if u.get("datum"):
        entry["datum"] = u["datum"]
    if u.get("ref_script"):
        lang = _SCRIPT_LANG.get(u.get("ref_script_type") or "", "plutus:v3")
        entry["script"] = {"language": lang, "cbor": u["ref_script"]}
    return entry


def _additional_utxo(fix: dict[str, Any]) -> list[dict[str, Any]]:
    return [_utxo_entry(u) for u in fix["inputs"] + fix["ref_inputs"]]


@_dbsync_gate()
def test_create_request_from_backend_resolves_capture_fields() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CreateRequestSnapshot,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
        RequestTerms,
    )

    fix = json.loads((FIXTURES_DIR / "create_request.json").read_text())
    cap = CreateRequestSnapshot.from_capture(fix)
    terms = RequestTerms.from_request_datum(
        RequestDatum.from_cbor(bytes.fromhex(cap.request_datum)),
    )
    backend = get_backend()
    snap = CreateRequestSnapshot.from_backend(
        backend,
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


@_dbsync_ogmios_gate("create_request.json")
def test_create_request_from_backend_evaluates_on_ogmios() -> None:
    from charli3_dendrite.backend import get_backend
    from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
    from charli3_dendrite.lending.fluidtokens.transactions.context import (
        CreateRequestSnapshot,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
        RequestTerms,
    )
    from charli3_dendrite.lending.fluidtokens.transactions.request import (
        build_create_request,
    )

    fix = json.loads((FIXTURES_DIR / "create_request.json").read_text())
    cap = CreateRequestSnapshot.from_capture(fix)
    terms = RequestTerms.from_request_datum(
        RequestDatum.from_cbor(bytes.fromhex(cap.request_datum)),
    )
    backend = get_backend()
    snap = CreateRequestSnapshot.from_backend(
        backend,
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
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["block_time"]))
    build_create_request(tx_builder, snapshot=snap)
    budgets = evaluate_tx_cbor(assemble_unsigned(tx_builder), _additional_utxo(fix))

    assert budgets, "ogmios returned no redeemer budgets"
    for entry in budgets:
        assert entry["budget"]["memory"] > 0 and entry["budget"]["cpu"] > 0
    purposes = sorted(e["validator"]["purpose"] for e in budgets)
    assert purposes == ["mint"]
