"""Typed errors raised while building and evaluating lending transactions.

`LendingBuildError` is the base of every build-time refusal; it subclasses
`ValueError`, which builders raised before these types existed.
`OgmiosEvaluationError` keeps the Ogmios ``evaluateTransaction`` error's code,
message and data, and is also an `AssertionError` with the message
``"ogmios evaluate error: <error>"`` that evaluation failures always carried, so
callers catching either type or matching that text keep working.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from collections.abc import Mapping
from typing import Any

__all__ = [
    "CANNOT_CREATE_EVALUATION_CONTEXT",
    "OVERLAPPING_ADDITIONAL_UTXO",
    "LendingBuildError",
    "OgmiosEvaluationError",
    "ValidityWindowError",
]

# Ogmios v6 ``OverlappingAdditionalUtxo``: an ``additionalUtxo`` entry that the
# node's ledger still holds.
OVERLAPPING_ADDITIONAL_UTXO = 3002

# Ogmios v6: the evaluation context could not be created, e.g. an input is not in
# the ledger's UTxO set (already spent or never created). It comes back as the
# top-level code, or nested in a script failure's per-validator errors.
CANNOT_CREATE_EVALUATION_CONTEXT = 3004

_MISSING_MARKER = "missing from UTxO set"
_OUT_REF_RE = re.compile(r"([0-9a-f]{64})#(\d+)")


class LendingBuildError(ValueError):
    """A lending transaction cannot be built as requested."""


class ValidityWindowError(LendingBuildError):
    """The transaction's validity window is empty or cannot be set as requested."""


class OgmiosEvaluationError(LendingBuildError, AssertionError):
    """Ogmios ``evaluateTransaction`` returned an error instead of budgets.

    Attributes:
        code: The Ogmios error code, or None when the body carries none.
        reason: The Ogmios error ``message``, or None.
        data: The Ogmios error ``data``, or None.
    """

    def __init__(self, error: Mapping[str, Any]) -> None:
        """Wrap an Ogmios JSON-RPC ``error`` object."""
        super().__init__(f"ogmios evaluate error: {error}")
        code = error.get("code")
        message = error.get("message")
        self.code: int | None = code if isinstance(code, int) else None
        self.reason: str | None = message if isinstance(message, str) else None
        self.data: Any = error.get("data")
        self._error = dict(error)

    def __reduce__(self) -> tuple[type[OgmiosEvaluationError], tuple[dict]]:
        """Rebuild from the Ogmios error object, so pickle and copy round-trip."""
        return type(self), (self._error,)

    def overlapping_refs(self) -> frozenset[tuple[str, int]]:
        """The out-refs a 3002 names as already on the ledger; empty otherwise."""
        if self.code != OVERLAPPING_ADDITIONAL_UTXO or not isinstance(
            self.data,
            Mapping,
        ):
            return frozenset()
        refs = self.data.get("overlappingOutputReferences") or []
        return frozenset(
            (str(ref["transaction"]["id"]), int(ref["index"])) for ref in refs
        )

    def missing_inputs(self) -> frozenset[tuple[str, int]]:
        """The out-refs the error reports missing from the UTxO set.

        Reads every ``<tx id>#<index>`` that follows ``missing from UTxO set`` in
        any text of the error: its message, and every string within its data,
        whatever the top-level code. A transaction whose only fault is a missing
        input comes back as a 3004 naming it in the message; one with redeemers
        comes back as a script failure (3010) whose per-validator ``error`` is a
        3004 naming it in ``data.reason``.
        """
        refs: set[tuple[str, int]] = set()
        for text in _texts(self.reason, self.data):
            start = text.find(_MISSING_MARKER)
            if start < 0:
                continue
            refs.update(
                (m.group(1), int(m.group(2))) for m in _OUT_REF_RE.finditer(text, start)
            )
        return frozenset(refs)


def _texts(*values: Any) -> Iterator[str]:  # noqa: ANN401
    """Every string in ``values``, descending into mappings and sequences."""
    stack = list(values)
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
