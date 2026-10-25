"""Live dbsync resolution of the UTxOs a FluidTokens pool action needs.

The capture path (`*Snapshot.from_capture`) parses a recorded tx; these helpers resolve
the same building blocks from current chain state via the backend's dbsync `db_query`.
`allow_spent=True` lets a caller resolve a historical (already-consumed) UTxO by its
out-ref -- used to replay a captured pool against live dbsync in tests; live callers
leave it False so only unspent UTxOs are returned.

The generic resolvers (`db_query_rows`, `utxo_assets`, `resolve_utxo_by_outref`,
`resolve_funding`) live in :mod:`charli3_dendrite.lending.transactions.resolve` and
are re-exported here; the FluidTokens lookups below resolve through this module's
`resolve_utxo_by_outref`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from charli3_dendrite.lending.fluidtokens.constants import PROTOCOL_CONFIG_NFT_NAME
from charli3_dendrite.lending.fluidtokens.constants import PROTOCOL_CONFIG_NFT_POLICY
from charli3_dendrite.lending.transactions.resolve import db_query_rows
from charli3_dendrite.lending.transactions.resolve import resolve_funding
from charli3_dendrite.lending.transactions.resolve import resolve_utxo_by_outref
from charli3_dendrite.lending.transactions.resolve import utxo_assets

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "db_query_rows",
    "resolve_config_utxo",
    "resolve_funding",
    "resolve_script_ref",
    "resolve_utxo_by_asset",
    "resolve_utxo_by_outref",
    "utxo_assets",
]


def resolve_config_utxo(
    backend: AbstractBackend,
    *,
    allow_spent: bool = False,
) -> Utxo:
    """Resolve the UTxO holding the protocol config NFT (most-recent first)."""
    return resolve_utxo_by_asset(
        backend,
        PROTOCOL_CONFIG_NFT_POLICY,
        PROTOCOL_CONFIG_NFT_NAME,
        allow_spent=allow_spent,
    )


def resolve_utxo_by_asset(
    backend: AbstractBackend,
    policy: str,
    name: str,
    *,
    allow_spent: bool = False,
) -> Utxo:
    """Resolve the newest UTxO holding the native asset ``policy`` + ``name``.

    Filters to unspent unless ``allow_spent`` is set (dbsync retains consumed rows, so a
    captured/historical bond UTxO can be replayed). Raises :class:`ValueError` when no
    matching output is found.
    """
    spent_filter = "" if allow_spent else "AND o.consumed_by_tx_id IS NULL"
    rows = db_query_rows(
        backend,
        f"""SELECT encode(t.hash, 'hex') AS tx_hash, o.index AS idx
            FROM tx_out o
            JOIN tx t ON t.id = o.tx_id
            JOIN ma_tx_out m ON m.tx_out_id = o.id
            JOIN multi_asset ma ON ma.id = m.ident
            WHERE ma.policy = decode(%(p)s, 'hex')
              AND ma.name = decode(%(n)s, 'hex')
              {spent_filter}
            ORDER BY o.id DESC
            LIMIT 1""",  # noqa: S608
        {"p": policy, "n": name},
    )
    if not rows:
        raise ValueError(f"no UTxO holding asset {policy}{name}")
    row = rows[0]
    return resolve_utxo_by_outref(
        backend,
        row["tx_hash"],
        int(row["idx"]),
        allow_spent=allow_spent,
    )


def resolve_script_ref(
    backend: AbstractBackend,
    script_hash: str,
    *,
    allow_spent: bool = False,
) -> Utxo:
    """Resolve the UTxO carrying the reference script with hash ``script_hash``."""
    spent_filter = "" if allow_spent else "AND o.consumed_by_tx_id IS NULL"
    rows = db_query_rows(
        backend,
        f"""SELECT encode(t.hash, 'hex') AS tx_hash, o.index AS idx
            FROM tx_out o
            JOIN tx t ON t.id = o.tx_id
            JOIN script s ON s.id = o.reference_script_id
            WHERE s.hash = decode(%(h)s, 'hex')
              {spent_filter}
            ORDER BY o.id DESC
            LIMIT 1""",  # noqa: S608
        {"h": script_hash},
    )
    if not rows:
        raise ValueError(f"no reference-script UTxO for hash {script_hash}")
    row = rows[0]
    return resolve_utxo_by_outref(
        backend,
        row["tx_hash"],
        int(row["idx"]),
        allow_spent=allow_spent,
    )
