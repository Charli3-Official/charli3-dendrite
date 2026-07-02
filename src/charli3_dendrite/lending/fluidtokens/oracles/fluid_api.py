"""Thin, optional client for the FluidTokens oracle witness.

A live BORROW or MODIFY_COLLATERAL must re-price its collateral through a signed,
time-bound "oracle reward" message that cannot be synthesized offline or replayed:
``BorrowSnapshot.from_backend`` / ``ChangeCollateralSnapshot.from_backend`` accept
that witness as INJECTED arguments (``oracle_reward_cbor`` plus the oracle feed and
oracle reference-script out-refs). This module is the ONLY network-touching seam that
produces those injected values from FluidTokens' public oracle registry; the
deterministic core (``from_backend`` and all offline tests) never imports or reaches it.

Using the client is entirely optional: a caller who already has a witness (e.g. from
the protocol owner, or captured on-chain) injects it into ``from_backend`` directly and
never touches this module.

CONFIRMED SCHEMA
----------------
The witness is assembled from a single public registry endpoint,
``GET /get-oracle-tokens`` on ``https://api.fluidtokens.com`` (verified live against the
protocol API). It returns one entry per oracle-priced token; the entry for the borrow
collateral carries everything needed:

* ``fluidOracle.referenceInput`` -> the oracle feed reference-input out-ref.
* ``fluidOracle.referenceScript`` -> the oracle withdraw reference-script out-ref.
* ``supportedOracle.multisig`` -> the current signed price window: ``validFrom`` /
  ``validTo`` (POSIX ms), ``tokenPriceInLovelaces`` / ``tokenPriceDenominator``, and a
  ``multisigOracle.signatures`` list of ``{publicKey, signature}`` pairs.

The signed ``oracle_reward_cbor`` redeemer is reconstructed from those parts via
:func:`build_oracle_reward_cbor`; the token identity in the signed message is the
collateral token itself. Only ``multisig`` oracles are supported (all FluidTokens
lending collaterals use them); ``c3``-backed oracles price from an on-chain Charli3 feed
and are not handled here. ``lender_bond_datum`` is not served by this endpoint and is
returned as ``None`` -- the borrow caller injects the ``Unit`` fallback / preimage.

Credentials are read from the environment and NEVER committed: ``FLUIDTOKENS_API_BASE``
(default ``https://api.fluidtokens.com``) and ``FLUIDTOKENS_API_KEY`` (sent as the
``x-api-key`` header).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from charli3_dendrite.lending.fluidtokens.oracles.witness import (
    build_oracle_reward_cbor,
)

DEFAULT_API_BASE = "https://api.fluidtokens.com"
ENV_API_BASE = "FLUIDTOKENS_API_BASE"
ENV_API_KEY = "FLUIDTOKENS_API_KEY"  # env var name, not a secret

ORACLE_TOKENS_PATH = "/get-oracle-tokens"
_MULTISIG_ORACLE = "multisig"

_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


class FluidTokensApiError(RuntimeError):
    """Raised when the FluidTokens registry cannot yield a usable witness.

    Covers the failure modes the caller must act on: missing credentials, a non-2xx
    HTTP response, a response whose shape does not match the registry, an unknown
    collateral token, or an unsupported oracle type. Each carries an actionable message.
    """


@dataclass(frozen=True)
class OracleWitnessBundle:
    """The injected pieces ``from_backend`` needs for an oracle-priced action.

    Mirrors the BORROW / MODIFY_COLLATERAL ``from_backend`` injected arguments:
    ``oracle_reward_cbor`` is the signed, time-bound reward redeemer (hex, replayed
    verbatim by the builder); ``oracle_feed_outref`` is the collateral oracle feed
    reference UTxO and ``oracle_script_ref_outref`` the oracle withdraw
    reference-script UTxO, each an ``(tx_hash, index)`` out-ref. ``lender_bond_datum``
    is the optional borrow-only lender-bond datum preimage (hex); the registry never
    serves it, so it is always ``None`` here and the borrow caller supplies it.
    """

    oracle_reward_cbor: str
    oracle_feed_outref: tuple[str, int]
    oracle_script_ref_outref: tuple[str, int]
    lender_bond_datum: str | None = None


class FluidTokensProviderClient:
    """Thin HTTP client that builds a fresh oracle witness bundle from the registry.

    Configuration comes from the environment unless overridden: ``api_base`` defaults to
    :data:`FLUIDTOKENS_API_BASE` (then :data:`DEFAULT_API_BASE`) and ``api_key`` to
    :data:`FLUIDTOKENS_API_KEY`. A missing key raises :class:`FluidTokensApiError` at
    construction with instructions to set the env vars or inject the witness directly.

    The client owns the credential/transport concerns; the registry-shape parsing and
    the reward reconstruction are isolated in the ``_select_entry`` / ``_bundle_from_*``
    helpers.
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
        principal_unit: str | None = None,
        borrower_address: str | None = None,
        principal_amount: int | None = None,
    ) -> OracleWitnessBundle:
        """Fetch a fresh, time-bound oracle witness for ``collateral_unit``.

        Returns an :class:`OracleWitnessBundle` ready to inject into
        ``BorrowSnapshot.from_backend`` / ``ChangeCollateralSnapshot.from_backend``.
        The witness is keyed solely by the collateral token (``policy + name`` hex, or
        ``policy.name``). The ``principal_unit`` / ``borrower_address`` /
        ``principal_amount`` args are accepted for call-site symmetry with the borrow
        resolver but do not affect the oracle lookup.

        Raises :class:`FluidTokensApiError` on a non-2xx response, an unexpected
        registry shape, an unknown collateral token, or a non-``multisig`` oracle.
        """
        tokens = self._get(ORACLE_TOKENS_PATH)
        entry = self._select_entry(tokens, collateral_unit)
        return self._bundle_from_entry(entry, collateral_unit=collateral_unit)

    def _select_entry(
        self,
        tokens: object,
        collateral_unit: str,
    ) -> dict[str, Any]:
        """Return the active registry entry whose token matches ``collateral_unit``.

        Raises :class:`FluidTokensApiError` if the registry is not a list, no active
        entry prices the collateral, or the matched entry is not a ``multisig`` oracle.
        """
        if not isinstance(tokens, list):
            raise FluidTokensApiError(
                "FluidTokens registry response was not a JSON array; cannot select an "
                f"oracle token (got {type(tokens).__name__}).",
            )
        want = _normalize_unit(collateral_unit)
        for entry in tokens:
            if not isinstance(entry, dict):
                continue
            token = entry.get("token") or {}
            unit = f"{token.get('policyId', '')}{token.get('assetName', '')}"
            if _normalize_unit(unit) != want:
                continue
            if not entry.get("active", True):
                raise FluidTokensApiError(
                    f"FluidTokens oracle for collateral {collateral_unit} is inactive.",
                )
            if _MULTISIG_ORACLE not in (entry.get("supportedOracle") or {}):
                raise FluidTokensApiError(
                    f"FluidTokens oracle for collateral {collateral_unit} is not a "
                    f"multisig oracle (preferred={entry.get('preferredOracle')!r}); "
                    "only multisig oracles are supported by this client.",
                )
            return entry
        raise FluidTokensApiError(
            "FluidTokens registry has no oracle token for collateral "
            f"{collateral_unit}.",
        )

    def _bundle_from_entry(
        self,
        entry: dict[str, Any],
        *,
        collateral_unit: str,
    ) -> OracleWitnessBundle:
        """Reconstruct an :class:`OracleWitnessBundle` from a ``multisig`` entry.

        Reads the reference out-refs from ``fluidOracle`` and rebuilds the signed reward
        redeemer from ``supportedOracle.multisig`` (window, price, signatures) with the
        collateral token as the priced token. Raises :class:`FluidTokensApiError` if a
        required field is missing or malformed.
        """
        fluid = entry.get("fluidOracle") or {}
        feed = _parse_outref(fluid.get("referenceInput"), what="oracle feed")
        script_ref = _parse_outref(
            fluid.get("referenceScript"),
            what="oracle script-ref",
        )

        token = entry.get("token") or {}
        multisig = (entry.get("supportedOracle") or {}).get(_MULTISIG_ORACLE) or {}
        public_keys = [
            str(pk).lower()
            for pk in (entry.get("multisigOracle") or {}).get("publicKeys", [])
        ]
        try:
            reward_cbor = build_oracle_reward_cbor(
                valid_from_ms=int(multisig["validFrom"]),
                valid_to_ms=int(multisig["validTo"]),
                collateral_policy=str(token["policyId"]),
                collateral_name=str(token["assetName"]),
                price_num=int(multisig["tokenPriceInLovelaces"]),
                price_den=int(multisig["tokenPriceDenominator"]),
                signatures=_parse_signatures(multisig, public_keys),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FluidTokensApiError(
                f"FluidTokens multisig oracle for collateral {collateral_unit} is "
                f"missing or malformed: {exc}",
            ) from exc

        return OracleWitnessBundle(
            oracle_reward_cbor=reward_cbor,
            oracle_feed_outref=feed,
            oracle_script_ref_outref=script_ref,
            lender_bond_datum=None,
        )

    def _get(self, path: str) -> Any:  # noqa: ANN401
        """GET ``path`` and return the decoded JSON body.

        Raises :class:`FluidTokensApiError` on a transport error, a non-2xx status, or a
        body that is not valid JSON -- each with the status/URL for diagnosis.
        """
        url = f"{self._api_base}{path}"
        try:
            response = self._session.get(
                url,
                headers={"x-api-key": self._api_key},
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


def _parse_signatures(
    multisig: dict[str, Any],
    public_keys: list[str],
) -> list[tuple[bytes, int]]:
    """Extract ``(signature, signer_index)`` pairs from a multisig oracle block.

    Each ``signer_index`` is the position of the signature's public key within the
    oracle's ordered ``publicKeys`` (falling back to encounter order when the key is
    absent from the list). Raises :class:`ValueError` if no signatures are present.
    """
    raw = (multisig.get("multisigOracle") or {}).get("signatures") or []
    signatures: list[tuple[bytes, int]] = []
    for position, item in enumerate(raw):
        signature = bytes.fromhex(str(item["signature"]))
        public_key = str(item.get("publicKey", "")).lower()
        index = public_keys.index(public_key) if public_key in public_keys else position
        signatures.append((signature, index))
    if not signatures:
        raise ValueError("no signatures in multisig oracle block")
    return signatures


def _normalize_unit(unit: str) -> str:
    """Normalize an asset unit for comparison: drop ``.`` separators, lowercase."""
    return unit.replace(".", "").lower()


def _parse_outref(value: Any, *, what: str) -> tuple[str, int]:  # noqa: ANN401
    """Coerce a registry out-ref into ``(tx_hash, index)``.

    Accepts the registry's ``"tx_hash#index"`` string plus the common mapping /
    two-item-pair encodings so a minor shape change is absorbed here. Raises
    :class:`FluidTokensApiError` if none apply.
    """
    if isinstance(value, str) and "#" in value:
        str_hash, _, str_index = value.partition("#")
        if str_hash and str_index.isdigit():
            return str_hash, int(str_index)
    elif isinstance(value, dict):
        raw_hash = value.get("txHash") or value.get("tx_hash") or value.get("hash")
        raw_index = value.get("index")
        if raw_index is None:
            raw_index = value.get("outputIndex", value.get("output_index"))
        if isinstance(raw_hash, str) and isinstance(raw_index, int):
            return raw_hash, raw_index
    elif isinstance(value, (list, tuple)) and len(value) == 2:  # noqa: PLR2004
        pair_hash, pair_index = value
        if isinstance(pair_hash, str) and isinstance(pair_index, int):
            return pair_hash, pair_index

    raise FluidTokensApiError(
        f"FluidTokens registry {what} out-ref is missing or malformed; expected a "
        "'tx_hash#index' string.",
    )
