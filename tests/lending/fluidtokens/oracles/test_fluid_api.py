"""Tests for the optional FluidTokens oracle-witness client.

OFFLINE unit tests (always run, never touch the network) drive the client through a
mocked ``requests.Session`` returning a realistic ``GET /get-oracle-tokens`` registry
payload and assert: a ``multisig`` entry reconstructs into a well-formed
``OracleWitnessBundle`` whose reward parses to the registry's window/price/token; a
missing API key raises the actionable error; and unknown-token / unsupported-oracle /
non-2xx / malformed responses raise ``FluidTokensApiError``. Env vars are cleared per
test so the suite never depends on ambient credentials.

The GATED integration test hits the live registry and is skipped unless BOTH
``FLUIDTOKENS_API_KEY`` and the explicit opt-in ``FLUIDTOKENS_API_LIVE`` are set -- so
merely having a key in a loaded ``.env`` does not fire a network call in the normal
suite. The endpoint / response shape is confirmed against the live Provider API, so the
test asserts a fully well-formed, currently-valid bundle (no ``xfail``).
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
from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward

SNEK_UNIT = "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f534e454b"
SNEK_PUBKEY = "CB1506C82C3143948618C50834A527B8D471EBAE067BC0A5DEE1627DC511E914"
SNEK_SIG = (
    "37e50ea9f1bc6fc3e94025c94bf18ee36e8c12d41ac74cd925ceb58e2622637d"
    "0f70b77c491955205e6367e208d543d6cabf17923ae6ac905bbf583e730ef10c"
)

# A trimmed, realistic ``GET /get-oracle-tokens`` payload: one multisig entry (SNEK)
# and one unsupported c3 entry (OADA).
REGISTRY = [
    {
        "token": {
            "policyId": "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f",
            "assetName": "534e454b",
        },
        "fluidOracle": {
            "referenceScript": (
                "2d557048a3750f00549641048cd82081a8f033479c5d0454886b224675d2c975#0"
            ),
            "referenceInput": (
                "9686bcd665a08de5e08132a22a1749734535d2cf785de9c19548bbea94890502#0"
            ),
        },
        "multisigOracle": {"publicKeys": [SNEK_PUBKEY], "requiredSignatures": 1},
        "preferredOracle": "multisig",
        "active": True,
        "supportedOracle": {
            "multisig": {
                "validFrom": 1783018500949,
                "validTo": 1783021500949,
                "tokenPriceInLovelaces": 205571,
                "tokenPriceDenominator": 100,
                "multisigOracle": {
                    "signatures": [
                        {"publicKey": SNEK_PUBKEY, "signature": SNEK_SIG},
                    ],
                    "requiredSignatures": 1,
                },
            },
        },
    },
    {
        "token": {
            "policyId": "f6099832f9563e4cf59602b3351c3c5a8a7dda2d44575ef69b82cf8d",
            "assetName": "",
        },
        "preferredOracle": "c3",
        "active": True,
        "supportedOracle": {"c3": {"validFrom": 1, "validTo": 2}},
    },
]


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
    """Records the last GET and returns a preset response; never hits the network."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self._response


def _client(response: _FakeResponse) -> tuple[FluidTokensProviderClient, _FakeSession]:
    session = _FakeSession(response)
    client = FluidTokensProviderClient(api_key="dummy-key", session=session)
    return client, session


def test_fetch_reconstructs_bundle_from_registry() -> None:
    client, session = _client(_FakeResponse(payload=REGISTRY))

    bundle = client.fetch_oracle_witness(
        collateral_unit=SNEK_UNIT,
        principal_unit="lovelace",
        borrower_address="addr1qxyz",
        principal_amount=1_000_000,
    )

    assert isinstance(bundle, OracleWitnessBundle)
    assert bundle.oracle_feed_outref == (
        "9686bcd665a08de5e08132a22a1749734535d2cf785de9c19548bbea94890502",
        0,
    )
    assert bundle.oracle_script_ref_outref == (
        "2d557048a3750f00549641048cd82081a8f033479c5d0454886b224675d2c975",
        0,
    )
    assert bundle.lender_bond_datum is None

    # The reconstructed reward carries the registry's signed window / price / token.
    reward = OracleReward.parse(bundle.oracle_reward_cbor)
    assert reward.valid_from_ms == 1783018500949
    assert reward.valid_to_ms == 1783021500949
    assert reward.price_num == 205571
    assert reward.price_den == 100
    assert reward.collateral_name == "534e454b"
    assert reward.signature.hex() == SNEK_SIG

    # A GET with the auth header reaches the registry endpoint.
    call = session.calls[0]
    assert call["url"].endswith("/get-oracle-tokens")
    assert call["headers"]["x-api-key"] == "dummy-key"


def test_fetch_accepts_dotted_unit() -> None:
    client, _ = _client(_FakeResponse(payload=REGISTRY))
    dotted = "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f.534e454b"

    bundle = client.fetch_oracle_witness(collateral_unit=dotted)

    assert OracleReward.parse(bundle.oracle_reward_cbor).collateral_name == "534e454b"


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


def test_unknown_collateral_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(payload=REGISTRY))

    with pytest.raises(FluidTokensApiError, match="no oracle token"):
        client.fetch_oracle_witness(collateral_unit="deadbeef")


def test_unsupported_c3_oracle_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(payload=REGISTRY))

    with pytest.raises(FluidTokensApiError, match="not a multisig oracle"):
        client.fetch_oracle_witness(
            collateral_unit="f6099832f9563e4cf59602b3351c3c5a8a7dda2d44575ef69b82cf8d",
        )


def test_non_2xx_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(status_code=502, text="bad gateway"))

    with pytest.raises(FluidTokensApiError, match="502"):
        client.fetch_oracle_witness(collateral_unit=SNEK_UNIT)


def test_non_json_body_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(raise_on_json=True))

    with pytest.raises(FluidTokensApiError, match="non-JSON"):
        client.fetch_oracle_witness(collateral_unit=SNEK_UNIT)


def test_non_list_body_raises_api_error() -> None:
    client, _ = _client(_FakeResponse(payload={"not": "a list"}))

    with pytest.raises(FluidTokensApiError, match="not a JSON array"):
        client.fetch_oracle_witness(collateral_unit=SNEK_UNIT)


def test_malformed_outref_raises_api_error() -> None:
    entry = {k: v for k, v in REGISTRY[0].items()}
    entry["fluidOracle"] = {
        "referenceInput": "no-hash-marker",
        "referenceScript": "x#0",
    }
    client, _ = _client(_FakeResponse(payload=[entry]))

    with pytest.raises(FluidTokensApiError, match="oracle feed"):
        client.fetch_oracle_witness(collateral_unit=SNEK_UNIT)


@pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get("FLUIDTOKENS_API_LIVE")),
    reason="live FluidTokens API: set FLUIDTOKENS_API_KEY and FLUIDTOKENS_API_LIVE=1 "
    "(and optionally FLUIDTOKENS_API_BASE) to run",
)
def test_live_fetch_returns_well_formed_bundle() -> None:
    """GATED: hits the live registry and validates a well-formed, current bundle.

    Confirms the ``GET /get-oracle-tokens`` endpoint / shape against the real API: the
    reconstructed reward parses, prices SNEK, carries a 64-byte signature, and its signed
    window is still in the future (a fresh, usable witness).
    """
    import time

    client = FluidTokensProviderClient()
    bundle = client.fetch_oracle_witness(collateral_unit=SNEK_UNIT)

    assert isinstance(bundle, OracleWitnessBundle)
    assert len(bundle.oracle_feed_outref[0]) == 64
    assert len(bundle.oracle_script_ref_outref[0]) == 64
    assert bundle.lender_bond_datum is None

    reward = OracleReward.parse(bundle.oracle_reward_cbor)
    assert reward.collateral_name == "534e454b"
    assert len(reward.signature) == 64
    assert reward.valid_to_ms > int(time.time() * 1000)
