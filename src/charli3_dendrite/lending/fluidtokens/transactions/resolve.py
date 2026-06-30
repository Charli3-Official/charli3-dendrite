"""Live dbsync resolution of the UTxOs a FluidTokens pool action needs.

The capture path (`*Snapshot.from_capture`) parses a recorded tx; these helpers resolve
the same building blocks from current chain state via the backend's dbsync `db_query`.
`allow_spent=True` lets a caller resolve a historical (already-consumed) UTxO by its
out-ref -- used to replay a captured pool against live dbsync in tests; live callers
leave it False so only unspent UTxOs are returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from charli3_dendrite.lending.fluidtokens.constants import PROTOCOL_CONFIG_NFT_NAME
from charli3_dendrite.lending.fluidtokens.constants import PROTOCOL_CONFIG_NFT_POLICY
from charli3_dendrite.lending.fluidtokens.transactions.context import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.context import _as_utxo

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend


def _assets(backend: AbstractBackend, tx_out_id: int) -> list[tuple[str, str, int]]:
    """The native assets on a tx_out, ordered canonically (policy, name).

    Mirrors the capture tool's asset ordering so a resolved `Utxo` compares equal to a
    captured one byte-for-byte.
    """
    rows = backend.db_query(
        """SELECT encode(ma.policy, 'hex') AS policy,
                  encode(ma.name, 'hex') AS name,
                  m.quantity AS quantity
           FROM ma_tx_out m
           JOIN multi_asset ma ON ma.id = m.ident
           WHERE m.tx_out_id = %(oid)s
           ORDER BY ma.policy, ma.name""",
        {"oid": tx_out_id},
    )
    return [(r["policy"], r["name"], int(r["quantity"])) for r in rows]


def resolve_utxo_by_outref(
    backend: AbstractBackend,
    tx_hash: str,
    index: int,
    *,
    allow_spent: bool = False,
) -> Utxo:
    """Resolve the tx_out at ``(tx_hash, index)`` into a :class:`Utxo`.

    Filters to unspent (``consumed_by_tx_id IS NULL``) unless ``allow_spent`` is set, in
    which case an already-consumed (historical) output can still be resolved -- dbsync
    retains consumed rows. Raises :class:`ValueError` when no matching output is found.
    """
    spent_filter = "" if allow_spent else "AND o.consumed_by_tx_id IS NULL"
    rows = backend.db_query(
        f"""SELECT o.id AS id,
                   o.value AS lovelace,
                   a.address AS address,
                   encode(d.bytes, 'hex') AS datum,
                   encode(s.bytes, 'hex') AS ref_script,
                   s.type AS ref_script_type
            FROM tx_out o
            JOIN tx t ON t.id = o.tx_id
            JOIN address a ON a.id = o.address_id
            LEFT JOIN datum d ON d.id = o.inline_datum_id
            LEFT JOIN script s ON s.id = o.reference_script_id
            WHERE t.hash = decode(%(h)s, 'hex')
              AND o.index = %(i)s
              {spent_filter}
            LIMIT 1""",  # noqa: S608
        {"h": tx_hash, "i": index},
    )
    if not rows:
        raise ValueError(f"no tx_out for out-ref ({tx_hash}, {index})")
    row = rows[0]
    return _as_utxo(
        {
            "address": row["address"],
            "lovelace": int(row["lovelace"]),
            "assets": _assets(backend, row["id"]),
            "datum": row["datum"],
            "ref_script": row["ref_script"],
            "ref_script_type": row["ref_script_type"],
            "out_ref": [tx_hash, index],
        },
    )


def resolve_config_utxo(
    backend: AbstractBackend,
    *,
    allow_spent: bool = False,
) -> Utxo:
    """Resolve the UTxO holding the protocol config NFT (most-recent first)."""
    spent_filter = "" if allow_spent else "AND o.consumed_by_tx_id IS NULL"
    rows = backend.db_query(
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
        {"p": PROTOCOL_CONFIG_NFT_POLICY, "n": PROTOCOL_CONFIG_NFT_NAME},
    )
    if not rows:
        raise ValueError("no UTxO holding the protocol config NFT")
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
    rows = backend.db_query(
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


def resolve_funding(
    backend: AbstractBackend,
    address: str,
    *,
    limit: int = 20,
) -> list[Utxo]:
    """Resolve the unspent UTxOs at ``address`` (most-recent first, up to ``limit``)."""
    rows = backend.db_query(
        """SELECT encode(t.hash, 'hex') AS tx_hash, o.index AS idx
           FROM tx_out o
           JOIN tx t ON t.id = o.tx_id
           JOIN address a ON a.id = o.address_id
           WHERE a.address = %(addr)s
             AND o.consumed_by_tx_id IS NULL
           ORDER BY o.id DESC
           LIMIT %(lim)s""",
        {"addr": address, "lim": limit},
    )
    return [resolve_utxo_by_outref(backend, r["tx_hash"], int(r["idx"])) for r in rows]
