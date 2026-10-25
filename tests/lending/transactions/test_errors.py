"""Typed build errors and the Ogmios evaluation client's error handling."""

from __future__ import annotations

import re
from typing import Any

import pytest

from charli3_dendrite.lending.transactions import infra
from charli3_dendrite.lending.transactions.errors import LendingBuildError
from charli3_dendrite.lending.transactions.errors import OgmiosEvaluationError
from charli3_dendrite.lending.transactions.errors import ValidityWindowError
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor

_TX_A = "aa" * 32
_TX_B = "bb" * 32
_TX_C = "cc" * 32

_WITHDRAW_FAILURE = {
    "code": 3010,
    "message": "m",
    "data": {"validator": {"index": 0, "purpose": "withdraw"}},
}

_OVERLAPPING = {
    "code": 3002,
    "message": "Some UTxO references given as additional UTxO overlap with the ledger.",
    "data": {
        "overlappingOutputReferences": [
            {"transaction": {"id": _TX_A}, "index": 0},
            {"transaction": {"id": _TX_B}, "index": 3},
        ],
    },
}

_MISSING = {
    "code": 3004,
    "message": (
        "Unable to create the evaluation context from the given transaction. "
        f"Some inputs are missing from UTxO set: {_TX_C}#2"
    ),
    "data": {},
}

# The body Ogmios returns for a transaction with redeemers and one input missing
# from its UTxO set: a script failure whose per-validator errors carry the 3004.
_NESTED_MISSING = {
    "code": 3010,
    "message": "Some scripts of the transactions terminated with error(s).",
    "data": [
        {
            "validator": {"index": 1, "purpose": "spend"},
            "error": {
                "code": 3110,
                "message": "Extraneous (non-required) redeemers found in the transaction.",
                "data": {"extraneousRedeemers": [{"index": 1, "purpose": "spend"}]},
            },
        },
        {
            "validator": {"index": 0, "purpose": "mint"},
            "error": {
                "code": 3004,
                "message": (
                    "Unable to create the evaluation context from the given "
                    "transaction."
                ),
                "data": {
                    "reason": (
                        "Unknown transaction input (missing from UTxO set): "
                        f"{_TX_C}#4"
                    ),
                },
            },
        },
    ],
}

_RESULT = [
    {"validator": {"index": 0, "purpose": "withdraw"}, "budget": {"memory": 1}},
]


def _entry(tx_id: str, index: int) -> dict[str, Any]:
    return {
        "transaction": {"id": tx_id},
        "index": index,
        "address": "addr",
        "value": {"ada": {"lovelace": 2_000_000}},
    }


def test_message_keeps_the_existing_evaluate_error_text():
    # Callers match the dict repr after the prefix: the withdraw-failure pattern the
    # FluidTokens V4 live tests build still matches.
    error = OgmiosEvaluationError(_WITHDRAW_FAILURE)
    pattern = (
        r"ogmios evaluate error.*'validator': \{'index': "
        f"{0}, 'purpose': 'withdraw'}}"
    )
    assert re.search(pattern, str(error))
    assert str(error) == f"ogmios evaluate error: {_WITHDRAW_FAILURE}"
    assert error.args == (str(error),)


def test_error_is_caught_as_assertion_and_value_error():
    with pytest.raises(AssertionError, match="ogmios evaluate error"):
        raise OgmiosEvaluationError(_WITHDRAW_FAILURE)
    with pytest.raises(ValueError, match="ogmios evaluate error"):
        raise OgmiosEvaluationError(_WITHDRAW_FAILURE)
    with pytest.raises(LendingBuildError):
        raise OgmiosEvaluationError(_WITHDRAW_FAILURE)


def test_error_carries_code_reason_and_data():
    error = OgmiosEvaluationError(_WITHDRAW_FAILURE)
    assert error.code == 3010
    assert error.reason == "m"
    assert error.data == {"validator": {"index": 0, "purpose": "withdraw"}}


def test_error_tolerates_a_body_without_code_or_message():
    error = OgmiosEvaluationError({"detail": "x"})
    assert error.code is None
    assert error.reason is None
    assert error.data is None
    assert error.overlapping_refs() == frozenset()
    assert error.missing_inputs() == frozenset()


def test_error_survives_pickle_and_copy():
    import copy
    import pickle

    error = OgmiosEvaluationError({"code": 3002, "message": "m", "data": {"k": 1}})
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error)):
        assert type(clone) is OgmiosEvaluationError
        assert (clone.code, clone.reason, clone.data) == (3002, "m", {"k": 1})
        assert str(clone) == str(error)


def test_validity_window_error_is_a_build_error():
    assert issubclass(ValidityWindowError, LendingBuildError)
    assert issubclass(LendingBuildError, ValueError)


def test_overlapping_refs_parses_a_3002_body():
    error = OgmiosEvaluationError(_OVERLAPPING)
    assert error.overlapping_refs() == frozenset({(_TX_A, 0), (_TX_B, 3)})
    assert error.missing_inputs() == frozenset()


def test_overlapping_refs_only_for_code_3002():
    body = dict(_OVERLAPPING, code=3010)
    assert OgmiosEvaluationError(body).overlapping_refs() == frozenset()


def test_missing_inputs_parses_a_3004_reason():
    error = OgmiosEvaluationError(_MISSING)
    assert error.missing_inputs() == frozenset({(_TX_C, 2)})
    assert error.overlapping_refs() == frozenset()


def test_missing_inputs_reads_the_data_text_too():
    body = {
        "code": 3004,
        "message": "Unable to create the evaluation context.",
        "data": {"error": f"missing from UTxO set: {_TX_A}#0, {_TX_B}#7"},
    }
    assert OgmiosEvaluationError(body).missing_inputs() == frozenset(
        {(_TX_A, 0), (_TX_B, 7)},
    )


def test_missing_inputs_ignores_refs_before_the_marker():
    body = {
        "code": 3004,
        "message": f"input {_TX_A}#1 is fine; missing from UTxO set: {_TX_C}#2",
    }
    assert OgmiosEvaluationError(body).missing_inputs() == frozenset({(_TX_C, 2)})


def test_missing_inputs_reads_a_3004_nested_in_a_script_failure():
    error = OgmiosEvaluationError(_NESTED_MISSING)
    assert error.code == 3010
    assert error.missing_inputs() == frozenset({(_TX_C, 4)})


def test_missing_inputs_reads_every_nested_error():
    body = {
        "code": 3010,
        "message": "Some scripts of the transactions terminated with error(s).",
        "data": [
            {
                "validator": {"index": i, "purpose": "spend"},
                "error": {
                    "code": 3004,
                    "data": {"reason": f"missing from UTxO set: {tx}#{i}"},
                },
            }
            for i, tx in enumerate([_TX_A, _TX_B])
        ],
    }
    assert OgmiosEvaluationError(body).missing_inputs() == frozenset(
        {(_TX_A, 0), (_TX_B, 1)},
    )


def test_missing_inputs_is_empty_for_a_script_failure_naming_no_input():
    assert OgmiosEvaluationError(_WITHDRAW_FAILURE).missing_inputs() == frozenset()


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def _fake_post(
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Answer each POST with the next body; return the posted payloads."""
    monkeypatch.setenv("OGMIOS_HOST", "localhost")
    sent: list[dict[str, Any]] = []
    queue = list(bodies)

    def post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        sent.append(json)
        return _Response(queue.pop(0))

    monkeypatch.setattr(infra.requests, "post", post)
    return sent


def _refs(payload: dict[str, Any]) -> list[tuple[str, int]]:
    return [
        (e["transaction"]["id"], e["index"])
        for e in payload["params"]["additionalUtxo"]
    ]


def test_evaluate_returns_the_result(monkeypatch):
    sent = _fake_post(monkeypatch, [{"result": _RESULT}])
    entries = [_entry(_TX_C, 1)]
    assert evaluate_tx_cbor("84", entries) == _RESULT
    assert len(sent) == 1
    assert sent[0]["params"]["transaction"] == {"cbor": "84"}
    assert _refs(sent[0]) == [(_TX_C, 1)]


def test_evaluate_retries_once_without_the_overlapping_entries(monkeypatch):
    sent = _fake_post(
        monkeypatch,
        [{"error": _OVERLAPPING}, {"result": _RESULT}],
    )
    entries = [_entry(_TX_A, 0), _entry(_TX_C, 1), _entry(_TX_B, 3)]
    assert evaluate_tx_cbor("84", entries) == _RESULT
    assert len(sent) == 2
    assert _refs(sent[0]) == [(_TX_A, 0), (_TX_C, 1), (_TX_B, 3)]
    assert _refs(sent[1]) == [(_TX_C, 1)]
    assert sent[1]["params"]["transaction"] == {"cbor": "84"}
    # The caller's list is left as given.
    assert [(e["transaction"]["id"], e["index"]) for e in entries] == [
        (_TX_A, 0),
        (_TX_C, 1),
        (_TX_B, 3),
    ]


def test_evaluate_raises_when_the_retry_fails(monkeypatch):
    sent = _fake_post(
        monkeypatch,
        [{"error": _OVERLAPPING}, {"error": _NESTED_MISSING}],
    )
    with pytest.raises(OgmiosEvaluationError) as raised:
        evaluate_tx_cbor("84", [_entry(_TX_A, 0), _entry(_TX_C, 2)])
    assert raised.value.code == 3010
    assert raised.value.missing_inputs() == frozenset({(_TX_C, 4)})
    assert len(sent) == 2


def test_evaluate_retries_at_most_once(monkeypatch):
    second = {
        "code": 3002,
        "message": "overlap",
        "data": {
            "overlappingOutputReferences": [
                {"transaction": {"id": _TX_C}, "index": 1},
            ],
        },
    }
    sent = _fake_post(monkeypatch, [{"error": _OVERLAPPING}, {"error": second}])
    with pytest.raises(OgmiosEvaluationError) as raised:
        evaluate_tx_cbor("84", [_entry(_TX_A, 0), _entry(_TX_C, 1)])
    assert raised.value.code == 3002
    assert len(sent) == 2


def test_evaluate_raises_other_errors_without_retrying(monkeypatch):
    sent = _fake_post(monkeypatch, [{"error": _WITHDRAW_FAILURE}])
    with pytest.raises(AssertionError, match="ogmios evaluate error"):
        evaluate_tx_cbor("84", [_entry(_TX_A, 0)])
    assert len(sent) == 1


def test_evaluate_does_not_retry_when_no_stashed_entry_overlaps(monkeypatch):
    # A 3002 naming nothing the request carried would only repeat the request.
    sent = _fake_post(monkeypatch, [{"error": _OVERLAPPING}])
    with pytest.raises(OgmiosEvaluationError) as raised:
        evaluate_tx_cbor("84", [_entry(_TX_C, 1)])
    assert raised.value.code == 3002
    assert len(sent) == 1
