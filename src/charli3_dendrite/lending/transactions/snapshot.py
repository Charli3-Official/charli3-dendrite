"""Protocol-agnostic pool-action snapshot base."""

from __future__ import annotations

from abc import ABC
from typing import Any


class PoolActionSnapshot(ABC):
    """Live building blocks shared by every pool action.

    Concrete protocol snapshots (e.g. Danogo's) subclass this and add protocol-specific
    UTxOs/fields. The actor (funding) input(s) are the only entries Ogmios cannot
    resolve from its own ledger snapshot -- they are plain wallet UTxOs that may already
    be spent -- so they are supplied as the `additionalUtxo` set. Those entries are only
    known once the action is contributed (they derive from the caller's action params),
    so `contribute` appends each via `add_actor_additional_utxo` and `additional_utxo`
    returns the collected set.     The stash lives here, in one place, so every protocol
    snapshot reuses it rather than re-implementing it.
    """

    # Declared (not assigned) so it is not collected as a dataclass field by dataclass
    # subclasses; the list is created lazily on first append.
    _additional_utxos: list[dict[str, Any]]

    def add_actor_additional_utxo(self, entry: dict[str, Any]) -> None:
        """Append an actor (funding) input's Ogmios `additionalUtxo` entry.

        Called while the action is contributed, once the funding input is known.
        Initialized lazily so dataclass subclasses need no cooperative `__init__`.
        """
        if not hasattr(self, "_additional_utxos"):
            self._additional_utxos = []
        self._additional_utxos.append(entry)

    def additional_utxo(self) -> list[dict[str, Any]]:
        """Ogmios `additionalUtxo` entries the action stashed for its actor inputs.

        Raises if nothing was stashed -- contribute must populate it first; an empty
        set would silently drop the funding input and is treated as a build error.
        """
        entries: list[dict[str, Any]] = getattr(self, "_additional_utxos", [])
        if not entries:
            raise RuntimeError(
                "additional_utxo requested before contribute() populated it",
            )
        return entries
