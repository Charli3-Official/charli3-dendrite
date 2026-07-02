"""Thin, optional client for the FluidTokens Provider API oracle witness.

A live BORROW or MODIFY_COLLATERAL must re-price its collateral through a signed,
time-bound "oracle reward" message that cannot be synthesized offline or replayed:
``BorrowSnapshot.from_backend`` / ``ChangeCollateralSnapshot.from_backend`` accept
that witness as INJECTED arguments (``oracle_reward_cbor`` plus the oracle feed and
oracle reference-script out-refs). This module is the ONLY network-touching seam that
produces those injected values from FluidTokens' Provider API; the deterministic core
(``from_backend`` and all offline tests) never imports or reaches it.

Using the client is entirely optional: a caller who already has a witness (e.g. from
the protocol owner, or captured on-chain) injects it into ``from_backend`` directly and
never touches this module.

UNVERIFIED SCHEMA
-----------------
The Provider API lives at ``https://api.fluidtokens.com`` and is authenticated with an
``x-api-key`` header. Its documented surface builds whole transactions server-side
(``POST /providers/pools/borrow|new|cancel|modify``) and exposes market/position reads;
there is NO documented endpoint that returns a raw signed oracle witness and no
documented modify-collateral endpoint. So both the exact endpoint used to obtain a raw
witness AND the response shape it returns are UNVERIFIED against the live API.

Every such assumption is quarantined in the small, clearly-marked ``_endpoint_*`` /
``_parse_*`` helpers below so that, once the real schema is confirmed, only those
helpers change -- never the public surface. The gated integration test
(``tests/lending/fluidtokens/oracles/test_fluid_api.py``) is the confirmation
mechanism: it runs only when ``FLUIDTOKENS_API_KEY`` is set and validates the live
response against a well-formed bundle.

Credentials are read from the environment and NEVER committed: ``FLUIDTOKENS_API_BASE``
(default ``https://api.fluidtokens.com``) and ``FLUIDTOKENS_API_KEY`` (sent verbatim as
the ``x-api-key`` header).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_API_BASE = "https://api.fluidtokens.com"
ENV_API_BASE = "FLUIDTOKENS_API_BASE"
ENV_API_KEY = "FLUIDTOKENS_API_KEY"  # env var name, not a secret

_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


class FluidTokensApiError(RuntimeError):
    """Raised when the FluidTokens Provider API cannot yield a usable witness.

    Covers the three failure modes the caller must act on: missing credentials, a
    non-2xx HTTP response, and a 2xx response whose shape does not match what the
    witness bundle needs. Each carries an actionable message.
    """


@dataclass(frozen=True)
class OracleWitnessBundle:
    """The injected pieces ``from_backend`` needs for an oracle-priced action.

    Mirrors the BORROW / MODIFY_COLLATERAL ``from_backend`` injected arguments:
    ``oracle_reward_cbor`` is the signed, time-bound reward redeemer (hex, replayed
    verbatim by the builder); ``oracle_feed_outref`` is the collateral oracle feed
    reference UTxO and ``oracle_script_ref_outref`` the oracle withdraw
    reference-script UTxO, each an ``(tx_hash, index)`` out-ref. ``lender_bond_datum``
    is the optional borrow-only lender-bond datum preimage (hex), ``None`` when the
    protocol commits to the Plutus ``Unit`` datum.
    """

    oracle_reward_cbor: str
    oracle_feed_outref: tuple[str, int]
    oracle_script_ref_outref: tuple[str, int]
    lender_bond_datum: str | None = None


class FluidTokensProviderClient:
    """Thin HTTP client that fetches a fresh oracle witness bundle.

    Configuration comes from the environment unless overridden: ``api_base`` defaults to
    :data:`FLUIDTOKENS_API_BASE` (then :data:`DEFAULT_API_BASE`) and ``api_key`` to
    :data:`FLUIDTOKENS_API_KEY`. A missing key raises :class:`FluidTokensApiError` at
    construction with instructions to set the env vars or inject the witness directly.

    The client is deliberately minimal: it owns the credential/transport concerns and
    delegates every UNVERIFIED endpoint / response-shape assumption to the isolated
    ``_endpoint_*`` / ``_parse_*`` helpers.
    """

    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Build a client, resolving credentials from args then the environment.

        Raises :class:`FluidTokensApiError` if no API key is available -- the
        deterministic ``from_backend`` path stays fully usable without this client, so
        the actionable remedy is to set the env vars or inject the witness directly.
        """
        resolved_base = api_base or os.environ.get(ENV_API_BASE) or DEFAULT_API_BASE
        resolved_key = api_key or os.environ.get(ENV_API_KEY)
        if not resolved_key:
            raise FluidTokensApiError(
                "FluidTokens API key is required to fetch an oracle witness. Set the "
                f"{ENV_API_KEY} environment variable (and optionally {ENV_API_BASE}), "
                "or skip this client entirely and inject oracle_reward_cbor / "
                "oracle_feed_outref / oracle_script_ref_outref directly into "
                "BorrowSnapshot.from_backend / ChangeCollateralSnapshot.from_backend.",
            )
        self._api_base = resolved_base.rstrip("/")
        self._api_key = resolved_key
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch_oracle_witness(
        self,
        *,
        collateral_unit: str,
        principal_unit: str,
        borrower_address: str | None = None,
        principal_amount: int | None = None,
    ) -> OracleWitnessBundle:
        """Fetch a fresh, time-bound oracle witness for a (collateral, principal).

        Returns an :class:`OracleWitnessBundle` ready to inject into
        ``BorrowSnapshot.from_backend`` / ``ChangeCollateralSnapshot.from_backend``.
        ``borrower_address`` / ``principal_amount`` are forwarded when present because
        the documented endpoints build a whole borrow tx server-side and may require
        them to produce a matching witness.

        UNVERIFIED against the live schema: the exact endpoint and the response shape
        parsed here MUST be confirmed against ``api.fluidtokens.com``; they are covered
        only by the gated integration test. Raises :class:`FluidTokensApiError` on a
        non-2xx response or a response whose shape does not yield a bundle.
        """
        path, payload = self._endpoint_and_payload(
            collateral_unit=collateral_unit,
            principal_unit=principal_unit,
            borrower_address=borrower_address,
            principal_amount=principal_amount,
        )
        body = self._post(path, payload)
        return self._parse_witness(body)

    # -- UNVERIFIED seam: endpoint + request shape ----------------------------------
    def _endpoint_and_payload(
        self,
        *,
        collateral_unit: str,
        principal_unit: str,
        borrower_address: str | None,
        principal_amount: int | None,
    ) -> tuple[str, dict[str, Any]]:
        """Build the (path, JSON body) for the witness request. UNVERIFIED.

        No documented endpoint returns a raw witness, so this targets the borrow
        builder (``/providers/pools/borrow``) from which a witness is expected to be
        extractable, sending the pair (and, when known, borrower/principal). Confirm
        the real path + body against the live API; only this method changes once known.
        """
        payload: dict[str, Any] = {
            "collateral": collateral_unit,
            "principal": principal_unit,
        }
        if borrower_address is not None:
            payload["address"] = borrower_address
        if principal_amount is not None:
            payload["amount"] = principal_amount
        return "/providers/pools/borrow", payload

    def _post(self, path: str, payload: dict[str, Any]) -> Any:  # noqa: ANN401
        """POST ``payload`` to ``path`` and return the decoded JSON body.

        Raises :class:`FluidTokensApiError` on a transport error, a non-2xx status, or a
        body that is not valid JSON -- each with the status/URL for diagnosis.
        """
        url = f"{self._api_base}{path}"
        try:
            response = self._session.post(
                url,
                json=payload,
                headers={
                    "x-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise FluidTokensApiError(
                f"FluidTokens API request to {url} failed: {exc}",
            ) from exc

        if not _HTTP_OK_MIN <= response.status_code < _HTTP_OK_MAX:
            raise FluidTokensApiError(
                f"FluidTokens API {url} returned {response.status_code}: "
                f"{response.text[:500]}",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise FluidTokensApiError(
                f"FluidTokens API {url} returned a non-JSON body: {exc}",
            ) from exc

    # -- UNVERIFIED seam: response shape --------------------------------------------
    def _parse_witness(self, body: Any) -> OracleWitnessBundle:  # noqa: ANN401
        """Parse a decoded response body into an :class:`OracleWitnessBundle`.

        UNVERIFIED against the live schema.

        The live field names are unconfirmed, so a small set of candidate keys is
        accepted for each piece (see ``_first``). Confirm against the real response and
        narrow these to the true keys; only this method + its helpers change. Raises
        :class:`FluidTokensApiError` if a required piece is absent or malformed.
        """
        if not isinstance(body, dict):
            raise FluidTokensApiError(
                "FluidTokens API response was not a JSON object; cannot extract an "
                f"oracle witness (got {type(body).__name__}).",
            )

        reward = _first(
            body,
            ("oracle_reward_cbor", "oracleRewardCbor", "oracleReward"),
        )
        if not isinstance(reward, str) or not reward:
            raise FluidTokensApiError(
                "FluidTokens API response missing the signed oracle reward cbor "
                "(expected a hex string under oracle_reward_cbor / oracleReward).",
            )

        feed = _parse_outref(
            _first(body, ("oracle_feed_outref", "oracleFeedOutRef", "oracleFeed")),
            what="oracle feed",
        )
        script_ref = _parse_outref(
            _first(
                body,
                (
                    "oracle_script_ref_outref",
                    "oracleScriptRefOutRef",
                    "oracleScriptRef",
                ),
            ),
            what="oracle script-ref",
        )

        lender_bond = _first(body, ("lender_bond_datum", "lenderBondDatum"))
        if lender_bond is not None and not isinstance(lender_bond, str):
            raise FluidTokensApiError(
                "FluidTokens API response lender bond datum must be a hex string.",
            )

        return OracleWitnessBundle(
            oracle_reward_cbor=reward,
            oracle_feed_outref=feed,
            oracle_script_ref_outref=script_ref,
            lender_bond_datum=lender_bond,
        )


def _first(body: dict[str, Any], keys: tuple[str, ...]) -> Any:  # noqa: ANN401
    """Return the first present value among ``keys`` (UNVERIFIED aliases), else None."""
    for key in keys:
        if key in body and body[key] is not None:
            return body[key]
    return None


def _parse_outref(value: Any, *, what: str) -> tuple[str, int]:  # noqa: ANN401
    """Coerce an out-ref into ``(tx_hash, index)``. UNVERIFIED shape.

    Accepts the common encodings so a schema change is absorbed here: a mapping
    (``{"txHash": ..., "index": ...}`` and snake/output aliases), a two-item
    ``[tx_hash, index]`` pair, or a ``"tx_hash#index"`` string. Raises
    :class:`FluidTokensApiError` if none apply.
    """
    if isinstance(value, dict):
        tx_hash = _first(value, ("tx_hash", "txHash", "transaction_id", "hash"))
        index = _first(value, ("index", "output_index", "outputIndex", "ix"))
        if isinstance(tx_hash, str) and isinstance(index, int):
            return tx_hash, index
    elif isinstance(value, (list, tuple)) and len(value) == 2:  # noqa: PLR2004
        tx_hash, index = value
        if isinstance(tx_hash, str) and isinstance(index, int):
            return tx_hash, index
    elif isinstance(value, str) and "#" in value:
        tx_hash, _, index = value.partition("#")
        if tx_hash and index.isdigit():
            return tx_hash, int(index)

    raise FluidTokensApiError(
        f"FluidTokens API response {what} out-ref is missing or malformed; expected "
        "a (tx_hash, index) mapping, pair, or 'tx_hash#index' string.",
    )
