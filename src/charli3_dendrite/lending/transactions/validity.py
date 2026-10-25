"""Validity-window merging for lending transactions built from several legs.

A builder's window is ``[validity_start, ttl)`` in slots: ``validity_start`` is the
first slot the transaction is valid in, ``ttl`` the first slot it is no longer
valid in (invalid-hereafter). Each leg that bounds the window intersects its bounds
with the ones already set instead of overwriting them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from charli3_dendrite.lending.transactions.errors import ValidityWindowError

if TYPE_CHECKING:
    from pycardano import TransactionBuilder

__all__ = ["merge_validity"]


def merge_validity(
    tx_builder: TransactionBuilder,
    *,
    upper_slot: int,
    lower_slot: int | None = None,
) -> None:
    """Intersect the builder's validity window with ``[lower_slot, upper_slot)``.

    The ttl becomes the smaller of the existing ttl and ``upper_slot``; the
    validity start the larger of the existing start and ``lower_slot``, where a
    ``None`` on either side leaves the other.

    Raises:
        ValidityWindowError: The merged window is empty (its start is at or past
            its ttl). The builder is left unchanged.
    """
    ttl = upper_slot if tx_builder.ttl is None else min(tx_builder.ttl, upper_slot)
    start = tx_builder.validity_start
    if lower_slot is not None:
        start = lower_slot if start is None else max(start, lower_slot)
    if start is not None and start >= ttl:
        raise ValidityWindowError(
            f"empty validity window: start slot {start} is not before ttl {ttl}",
        )
    tx_builder.ttl = ttl
    tx_builder.validity_start = start
