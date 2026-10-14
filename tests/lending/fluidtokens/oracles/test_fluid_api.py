"""Tests for the optional FluidTokens oracle-witness client.

OFFLINE unit tests (always run, never touch the network) drive the client through a
mocked ``requests.Session`` returning a realistic ``GET /get-oracle-tokens`` registry
payload and assert: a ``multisig`` entry reconstructs into a well-formed
``OracleWitnessBundle`` whose reward parses to the registry's window/price/token; the
public registry is read with or without an API key; a token listed once per oracle
version yields the entry of the oracle asked for; and unknown-token /
unsupported-oracle / non-2xx / malformed responses raise ``FluidTokensApiError``.

The GATED integration tests hit the live registry and are skipped unless the explicit
opt-in ``FLUIDTOKENS_API_LIVE`` is set, so the normal suite never makes a network call.
The registry is public: they run without a key (``FLUIDTOKENS_API_KEY`` is sent when
set). The endpoint / response shape is confirmed against the live Provider API, so the
tests assert a fully well-formed, currently-valid bundle (no ``xfail``), taken from the
entry of the oracle asked for.
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


def test_the_registry_is_read_without_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The registry endpoint is public: no key, no key header.
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_API_BASE, raising=False)
    session = _FakeSession(_FakeResponse(payload=REGISTRY))
    client = FluidTokensProviderClient(session=session)

    bundle = client.fetch_oracle_witness(collateral_unit=SNEK_UNIT)

    assert OracleReward.parse(bundle.oracle_reward_cbor).collateral_name == "534e454b"
    (call,) = session.calls
    assert call["url"] == "https://api.fluidtokens.com/get-oracle-tokens"
    assert "x-api-key" not in call["headers"]


def test_an_api_key_from_the_environment_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_API_KEY, "env-key")
    session = _FakeSession(_FakeResponse(payload=REGISTRY))
    FluidTokensProviderClient(session=session).fetch_oracle_witness(
        collateral_unit=SNEK_UNIT,
    )
    assert session.calls[0]["headers"]["x-api-key"] == "env-key"


# The SNEK oracle NFTs of the two oracle versions FluidTokens publishes side by side.
SNEK_ORACLE_V1 = "93794f9b7f3dc632cb889c7aec7d334f016f532e64f16141b6895f5b"
SNEK_ORACLE_V2 = "26e60b2083c14b849e622f8e05dd46ab01a7986fe5d72eeba8680d26"
SNEK_ORACLE_NAME = "6f7261636c65534e454b4333"
SNEK_V2_FEED = "4e4b5381376bae774459510b16b3cf4b52cf7437addf88d5dd24d167080d9dcd"
SNEK_V2_SCRIPT = "90dce925fda4441f5f4adc9c82ceab22a938b0f717df56853cb518bc114db7b5"


def _versioned(*, v1_active: bool = True) -> list[dict[str, Any]]:
    """SNEK listed once per oracle version, version 1 first, as the registry does."""
    v1 = {**REGISTRY[0], "oracleVersion": 1, "active": v1_active}
    v1["fluidOracle"] = {
        **REGISTRY[0]["fluidOracle"],
        "policyId": SNEK_ORACLE_V1,
        "assetName": SNEK_ORACLE_NAME,
    }
    v2 = {**REGISTRY[0], "oracleVersion": 2}
    v2["fluidOracle"] = {
        "policyId": SNEK_ORACLE_V2,
        "assetName": SNEK_ORACLE_NAME,
        "referenceScript": f"{SNEK_V2_SCRIPT}#1",
        "referenceInput": f"{SNEK_V2_FEED}#1",
    }
    return [v1, v2, REGISTRY[1]]


@pytest.mark.parametrize(
    ("oracle_token", "feed", "script"),
    [
        (
            SNEK_ORACLE_V1 + SNEK_ORACLE_NAME,
            (REGISTRY[0]["fluidOracle"]["referenceInput"][:64], 0),
            (REGISTRY[0]["fluidOracle"]["referenceScript"][:64], 0),
        ),
        (SNEK_ORACLE_V2 + SNEK_ORACLE_NAME, (SNEK_V2_FEED, 1), (SNEK_V2_SCRIPT, 1)),
        (
            f"{SNEK_ORACLE_V2}.{SNEK_ORACLE_NAME}",
            (SNEK_V2_FEED, 1),
            (SNEK_V2_SCRIPT, 1),
        ),
        (None, (REGISTRY[0]["fluidOracle"]["referenceInput"][:64], 0), None),
    ],
    ids=["version-1", "version-2", "version-2-dotted", "first-listed"],
)
def test_fetch_takes_the_entry_of_the_oracle_asked_for(
    oracle_token: str | None,
    feed: tuple[str, int],
    script: tuple[str, int] | None,
) -> None:
    client, _ = _client(_FakeResponse(payload=_versioned()))

    bundle = client.fetch_oracle_witness(
        collateral_unit=SNEK_UNIT,
        oracle_token=oracle_token,
    )

    assert bundle.oracle_feed_outref == feed
    if script is not None:
        assert bundle.oracle_script_ref_outref == script
    # Both versions carry the same signed price.
    assert OracleReward.parse(bundle.oracle_reward_cbor).signature.hex() == SNEK_SIG


def test_fetch_skips_other_versions_of_the_oracle_asked_for() -> None:
    # An inactive version-1 entry does not stand in the way of version 2.
    client, _ = _client(_FakeResponse(payload=_versioned(v1_active=False)))

    bundle = client.fetch_oracle_witness(
        collateral_unit=SNEK_UNIT,
        oracle_token=SNEK_ORACLE_V2 + SNEK_ORACLE_NAME,
    )

    assert bundle.oracle_feed_outref == (SNEK_V2_FEED, 1)
    with pytest.raises(FluidTokensApiError, match="inactive"):
        client.fetch_oracle_witness(
            collateral_unit=SNEK_UNIT,
            oracle_token=SNEK_ORACLE_V1 + SNEK_ORACLE_NAME,
        )


def test_fetch_refuses_an_oracle_the_registry_does_not_list() -> None:
    client, _ = _client(_FakeResponse(payload=_versioned()))
    unlisted = "ff" * 28 + SNEK_ORACLE_NAME

    with pytest.raises(FluidTokensApiError, match=f"no oracle token .*{unlisted}"):
        client.fetch_oracle_witness(collateral_unit=SNEK_UNIT, oracle_token=unlisted)


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


live = pytest.mark.skipif(
    not os.environ.get("FLUIDTOKENS_API_LIVE"),
    reason="live FluidTokens API: set FLUIDTOKENS_API_LIVE=1 (and optionally "
    "FLUIDTOKENS_API_BASE / FLUIDTOKENS_API_KEY) to run",
)


class _RecordingSession:
    """A live ``requests`` session that keeps each request's headers and payload."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()
        self.headers: list[dict[str, str]] = []
        self.payloads: list[Any] = []

    def get(self, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        self.headers.append(dict(kwargs.get("headers") or {}))
        response = self._session.get(url, **kwargs)
        self.payloads.append(response.json())
        return response


def _unit_of(asset: dict[str, str] | None) -> str:
    """The ``policy + name`` unit of a registry asset object."""
    asset = asset or {}
    return f"{asset.get('policyId', '')}{asset.get('assetName', '')}"


def _assert_current_snek_bundle(bundle: OracleWitnessBundle) -> None:
    """A well-formed witness of SNEK whose signed window is still open."""
    import time

    assert isinstance(bundle, OracleWitnessBundle)
    assert len(bundle.oracle_feed_outref[0]) == 64
    assert len(bundle.oracle_script_ref_outref[0]) == 64
    assert bundle.lender_bond_datum is None

    reward = OracleReward.parse(bundle.oracle_reward_cbor)
    assert reward.collateral_name == "534e454b"
    assert len(reward.signature) == 64
    assert reward.valid_to_ms > int(time.time() * 1000)


@live
def test_live_fetch_returns_well_formed_bundle() -> None:
    """GATED: hits the live registry and validates a well-formed, current bundle.

    Confirms the ``GET /get-oracle-tokens`` endpoint / shape against the real API: the
    reconstructed reward parses, prices SNEK, carries a 64-byte signature, and its signed
    window is still in the future (a fresh, usable witness).
    """
    client = FluidTokensProviderClient()
    _assert_current_snek_bundle(client.fetch_oracle_witness(collateral_unit=SNEK_UNIT))


@live
def test_live_fetch_without_a_key_takes_the_oracle_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATED: the public registry, read without a key, serves the version-2 oracle.

    The registry lists SNEK once per oracle version; the bundle comes from the entry
    whose ``fluidOracle`` NFT is the version-2 SNEK oracle's.
    """
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    session = _RecordingSession()
    client = FluidTokensProviderClient(session=session)  # type: ignore[arg-type]
    oracle_token = SNEK_ORACLE_V2 + SNEK_ORACLE_NAME

    bundle = client.fetch_oracle_witness(
        collateral_unit=SNEK_UNIT,
        oracle_token=oracle_token,
    )

    _assert_current_snek_bundle(bundle)
    (headers,) = session.headers
    assert "x-api-key" not in headers
    (payload,) = session.payloads
    (oracle,) = [
        e["fluidOracle"]
        for e in payload
        if _unit_of(e.get("token")) == SNEK_UNIT
        and _unit_of(e.get("fluidOracle")) == oracle_token
    ]
    tx_hash, index = oracle["referenceInput"].split("#")
    assert bundle.oracle_feed_outref == (tx_hash, int(index))
