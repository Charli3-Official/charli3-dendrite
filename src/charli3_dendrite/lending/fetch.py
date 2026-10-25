"""Backend reads shared by the protocol loaders.

A protocol loader fetches every unspent UTxO at a script address and classifies each
row with its own parser; the paging, the out-ref de-duplication and the default
evaluation time are the same for every protocol and live here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo

PAGE_SIZE: Final[int] = 1000


def unspent_utxos(
    backend: AbstractBackend,
    address: str,
    *,
    assets: Sequence[str] | None = None,
    page_size: int = PAGE_SIZE,
) -> list[PoolStateInfo]:
    """Every unspent UTxO the backend returns for ``address``, once per out-ref.

    Pages through ``get_pool_utxos`` and terminates on a short page or on a page that
    adds no new out-ref, so a backend that ignores ``page`` and returns the same rows
    again ends the loop. Rows are de-duplicated by out-ref, keeping the first row
    returned, since the backend pages with LIMIT / OFFSET. Rows keep the backend's
    order. ``assets``, when given, is passed on as the backend's asset filter; the
    termination counts the rows returned, whatever the caller keeps of them.
    """
    query: dict[str, Any] = {"addresses": [address], "historical": False}
    if assets is not None:
        query["assets"] = list(assets)
    rows_by_ref: dict[tuple[str, int], PoolStateInfo] = {}
    page = 0
    while True:
        rows = list(backend.get_pool_utxos(limit=page_size, page=page, **query))
        before = len(rows_by_ref)
        for info in rows:
            rows_by_ref.setdefault((info.tx_hash, info.tx_index), info)
        if len(rows) < page_size or len(rows_by_ref) == before:
            return list(rows_by_ref.values())
        page += 1


def resolve_now_ms(now_ms: int | None) -> int:
    """``now_ms`` (POSIX ms), or the current time when it is None."""
    if now_ms is None:
        return int(time.time() * 1000)
    return now_ms
