"""Offline: `LendSnapshot.from_backend` defaults, with a stubbed backend.

These exercise the live-resolution defaults without a dbsync/ogmios backend: the
``resolve_*`` helpers and the tip accessor are monkeypatched to feed the captured
request UTxO from ``lend.json``, so only the default-derivation logic under test runs
(principal default + bounds guard, min-ADA loan-output floor, stake carry-over).
"""

from __future__ import annotations

import json
from pathlib import Path

import charli3_dendrite.lending.fluidtokens.transactions.resolve as resolve_mod
import charli3_dendrite.lending.transactions.infra as infra_mod
import pytest
from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    request_principal_bounds,
)
from pycardano import Address

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_TIP = 1_000_000


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture()
def captured() -> LendSnapshot:
    return LendSnapshot.from_capture(_fixture("lend.json"))


@pytest.fixture()
def stub_backend(monkeypatch, captured: LendSnapshot) -> LendSnapshot:
    """Stub the live resolvers + tip so `from_backend` runs offline.

    ``resolve_utxo_by_outref`` returns the captured request UTxO; the config / script
    refs return any resolved UTxO (their identity is irrelevant to the defaults under
    test); the tip is a fixed slot. Returns the captured snapshot for the assertions.
    """
    request = captured.request
    monkeypatch.setattr(resolve_mod, "resolve_utxo_by_outref", lambda *a, **k: request)
    monkeypatch.setattr(
        resolve_mod, "resolve_config_utxo", lambda *a, **k: captured.config
    )
    monkeypatch.setattr(
        resolve_mod, "resolve_script_ref", lambda *a, **k: captured.config
    )
    monkeypatch.setattr(resolve_mod, "resolve_funding", lambda *a, **k: [])
    monkeypatch.setattr(infra_mod, "current_slot", lambda backend: _TIP)
    return captured


def _bounds(captured: LendSnapshot) -> tuple[int, int]:
    datum = RequestDatum.from_cbor(bytes.fromhex(captured.request.datum))
    collateral = next(
        (p, n, q) for p, n, q in captured.request.assets if p != captured.request_policy
    )
    return request_principal_bounds(datum, collateral_amount=collateral[2])


def test_from_backend_defaults_principal_to_max(stub_backend: LendSnapshot) -> None:
    _min_principal, max_principal = _bounds(stub_backend)
    snapshot = LendSnapshot.from_backend(object(), request_utxo=("aa", 0))
    assert snapshot.given_principal_amount == max_principal


def test_from_backend_rejects_out_of_bounds_principal(
    stub_backend: LendSnapshot,
) -> None:
    _min_principal, max_principal = _bounds(stub_backend)
    with pytest.raises(ValueError, match="outside the request's static bounds"):
        LendSnapshot.from_backend(
            object(),
            request_utxo=("aa", 0),
            given_principal_amount=max_principal + 1,
        )


def test_from_backend_defaults_loan_lovelace_to_min_ada(
    stub_backend: LendSnapshot,
) -> None:
    # The captured loan output's coin is exactly its protocol min-ADA, so the live
    # default (computed via min_lovelace) must reproduce it -- and it must NOT be the
    # old flat 2_000_000, which is below the loan output's min-UTxO.
    snapshot = LendSnapshot.from_backend(object(), request_utxo=("aa", 0))
    assert snapshot.loan_lovelace == stub_backend.loan_lovelace
    assert snapshot.loan_lovelace > 2_000_000


def test_from_backend_carries_request_stake_into_loan_address(
    stub_backend: LendSnapshot,
) -> None:
    snapshot = LendSnapshot.from_backend(object(), request_utxo=("aa", 0))
    request_stake = Address.decode(stub_backend.request.address).staking_part
    assert Address.decode(snapshot.loan_address).staking_part == request_stake
