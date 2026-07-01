"""Lending transaction-builder registry.

Maps a canonical protocol identifier to its `AbstractLendingTxBuilder` subclass.
The class (not an instance) is registered, because a builder takes a backend /
config at construction; callers look up the class and instantiate it themselves.

This mirrors the oracle resolver registry in `lending/oracles/models.py`:
built-in builders register at import time and are dispatched by name. The
registry is an additive convenience -- it does not replace direct construction
(`DanogoTxBuilder()`), and existing call sites keep working unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from charli3_dendrite.lending.transactions.base import AbstractLendingTxBuilder


_REGISTRY: dict[str, type[AbstractLendingTxBuilder]] = {}


def register_lending_builder(
    name: str,
    cls: type[AbstractLendingTxBuilder],
) -> None:
    """Register (or replace) the builder class for a protocol name.

    `name` is canonicalized to lowercase so lookups are case-insensitive.
    """
    _REGISTRY[name.lower()] = cls


def get_lending_builder(name: str) -> type[AbstractLendingTxBuilder]:
    """Return the builder class registered under `name`.

    Raises:
        KeyError: if no builder is registered for `name`, naming the known
            protocols to aid discovery.
    """
    key = name.lower()
    try:
        return _REGISTRY[key]
    except KeyError:
        known = ", ".join(available_lending_protocols()) or "(none registered)"
        raise KeyError(
            f"no lending builder registered for {name!r}; " f"known protocols: {known}",
        ) from None


def available_lending_protocols() -> list[str]:
    """Return the registered protocol names, sorted."""
    return sorted(_REGISTRY)


def _register_builtin() -> None:
    """Register the builders shipped with the library.

    Imported lazily so the registry module has no import-time dependency on any
    protocol package, keeping registration cycle-free.
    """
    from charli3_dendrite.lending.danogo.transactions.builder import DanogoTxBuilder

    register_lending_builder("danogo", DanogoTxBuilder)


_register_builtin()
