"""Tests for the optional FluidTokens Provider API oracle-witness client.

OFFLINE unit tests (always run, never touch the network) drive the client through a
mocked ``requests.Session`` and assert: a canned response parses into an
``OracleWitnessBundle``; a missing API key raises the actionable error; and a non-2xx /
malformed response raises ``FluidTokensApiError``. Env vars are cleared per test so the
suite never depends on ambient credentials.

The GATED integration test hits the live API and is skipped unless BOTH
``FLUIDTOKENS_API_KEY`` and the explicit opt-in ``FLUIDTOKENS_API_LIVE`` are set -- so
merely having a key in a loaded ``.env`` does not fire a network call in the normal
suite. Because the raw-witness endpoint / response shape is UNVERIFIED against the live
Provider API (there is no documented raw signed-feed endpoint), the test is marked
``xfail`` (non-strict): it records the expectation until the schema is confirmed, and
flips to XPASS once the guessed endpoint/shape actually matches. It is expected to skip
in CI / offline runs.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import ENV_API_BASE
from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import ENV_API_KEY
from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import FluidTokensApiError
from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
    FluidTokensProviderClient,
)
from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import OracleWitnessBundle

CANNED_WITNESS = {
    "oracle_reward_cbor": "d8799fd8799f00ff00ff",
    "oracle_feed_outref": {
        "txHash": "aa" * 32,
        "index": 0,
    },
    "oracle_script_ref_outref": {
        "txHash": "bb" * 32,
        "index": 1,
    },
    "lender_bond_datum": "d87980",
}


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by the mocked session."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        text: str = "",
        raise_on_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._raise_on_json = raise_on_json

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    """Records the last POST and returns a preset response; never hits the network."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self._response


def _client(response: _FakeResponse) -> tuple[FluidTokensProviderClient, _FakeSession]:
    session = _FakeSession(response)
    client = FluidTokensProviderClient(api_key="dummy-key", session=session)
    return client, session


def test_fetch_parses_canned_response() -> None:
    client, session = _client(_FakeResponse(payload=CANNED_WITNESS))

    bundle = client.fetch_oracle_witness(
        collateral_unit="279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f534e454b",
        principal_unit="lovelace",
        borrower_address="addr1qxyz",
        principal_amount=1_000_000,
    )

    assert bundle == OracleWitnessBundle(
        oracle_reward_cbor="d8799fd8799f00ff00ff",
        oracle_feed_outref=("aa" * 32, 0),
        oracle_script_ref_outref=("bb" * 32, 1),
        lender_bond_datum="d87980",
    )
    # Auth header + forwarded params reach the transport.
    call = session.calls[0]
    assert call["headers"]["x-api-key"] == "dummy-key"
    assert call["json"]["collateral"].endswith("534e454b")
    assert call["json"]["amount"] == 1_000_000


def test_fetch_parses_without_optional_lender_bond() -> None:
    payload = {k: v for k, v in CANNED_WITNESS.items() if k != "lender_bond_datum"}
    client, _ = _client(_FakeResponse(payload=payload))

    bundle = client.fetch_oracle_witness(
        collateral_unit="deadbeef",
        principal_unit="lovelace",
    )

    assert bundle.lender_bond_datum is None
    assert bundle.oracle_feed_outref == ("aa" * 32, 0)


def test_missing_api_key_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear ambient credentials so the no-key path is exercised regardless of a
    # locally-loaded .env; the other offline tests pass an explicit api_key and so are
    # already env-independent.
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_API_BASE, raising=False)
    with pytest.raises(FluidTokensApiError, match=ENV_API_KEY):
        FluidTokensProviderClient()


def test_non_2xx_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(status_code=502, text="bad gateway"))

    with pytest.raises(FluidTokensApiError, match="502"):
        client.fetch_oracle_witness(
            collateral_unit="deadbeef", principal_unit="lovelace"
        )


def test_non_json_body_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(raise_on_json=True))

    with pytest.raises(FluidTokensApiError, match="non-JSON"):
        client.fetch_oracle_witness(
            collateral_unit="deadbeef", principal_unit="lovelace"
        )


def test_missing_reward_cbor_raises_api_error() -> None:
    payload = {k: v for k, v in CANNED_WITNESS.items() if k != "oracle_reward_cbor"}
    client, _ = _client(_FakeResponse(payload=payload))

    with pytest.raises(FluidTokensApiError, match="oracle reward cbor"):
        client.fetch_oracle_witness(
            collateral_unit="deadbeef", principal_unit="lovelace"
        )


def test_malformed_outref_raises_api_error() -> None:
    payload = dict(CANNED_WITNESS)
    payload["oracle_feed_outref"] = {"txHash": "aa" * 32}  # missing index
    client, _ = _client(_FakeResponse(payload=payload))

    with pytest.raises(FluidTokensApiError, match="oracle feed"):
        client.fetch_oracle_witness(
            collateral_unit="deadbeef", principal_unit="lovelace"
        )


def test_non_object_body_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(payload=["not", "an", "object"]))

    with pytest.raises(FluidTokensApiError, match="not a JSON object"):
        client.fetch_oracle_witness(
            collateral_unit="deadbeef", principal_unit="lovelace"
        )


@pytest.mark.xfail(
    reason="raw-witness endpoint / response shape is unverified against the live "
    "FluidTokens Provider API (no documented raw signed-feed endpoint); XPASS when the "
    "guessed endpoint/shape is confirmed -- see STEEL-540",
    strict=False,
)
@pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get("FLUIDTOKENS_API_LIVE")),
    reason="live FluidTokens API: set FLUIDTOKENS_API_KEY and FLUIDTOKENS_API_LIVE=1 "
    "(and optionally FLUIDTOKENS_API_BASE) to run",
)
def test_live_fetch_returns_well_formed_bundle() -> None:
    """GATED: hits the live Provider API and validates a well-formed bundle.

    Confirms the UNVERIFIED endpoint / response shape against the real API. When a
    reward cbor comes back it is parsed and its signed window asserted to be in the
    future; both assumptions are the ones the offline tests cannot cover. Marked
    ``xfail`` until the endpoint/shape is confirmed (see module docstring).
    """
    import time

    from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward

    client = FluidTokensProviderClient()
    bundle = client.fetch_oracle_witness(
        collateral_unit="279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f534e454b",
        principal_unit="lovelace",
    )

    assert isinstance(bundle, OracleWitnessBundle)
    assert isinstance(bundle.oracle_reward_cbor, str) and bundle.oracle_reward_cbor
    assert len(bundle.oracle_feed_outref[0]) == 64
    assert len(bundle.oracle_script_ref_outref[0]) == 64

    reward = OracleReward.parse(bundle.oracle_reward_cbor)
    assert len(reward.signature) == 64
    assert reward.valid_to_ms > int(time.time() * 1000)
