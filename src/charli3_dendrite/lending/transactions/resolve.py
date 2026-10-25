"""Live dbsync resolution of the UTxOs a lending transaction needs.

The capture path (``*Snapshot.from_capture``) parses a recorded transaction; these
helpers resolve the same building blocks from current chain state through the
backend's dbsync ``db_query``: an output by its out-ref (with its typed reference
script and, for an output without an inline datum, its datum hash) and the unspent
outputs at a wallet address. ``allow_spent=True`` lets a caller resolve a historical
(already consumed) output by its out-ref, which replays a captured transaction against
live dbsync; live callers leave it ``False`` so only unspent outputs are returned.

Live resolution requires a dbsync-backed backend: ``db_query`` is a dbsync-only
method, so a different backend raises a clear :class:`TypeError` rather than an opaque
attribute error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

__all__ = [
    "db_query_rows",
    "resolve_funding",
    "resolve_utxo_by_outref",
    "utxo_assets",
]


def db_query_rows(
    backend: AbstractBackend,
    sql: str,
    args: dict[str, Any],
) -> list[dict]:
    """Run a dbsync query, failing loudly if the backend is not dbsync-backed.

    ``db_query`` is implemented only by the dbsync backend; a different backend has
    no such method, so raise a clear :class:`TypeError` instead of an opaque
    ``AttributeError`` deep inside a resolver.
    """
    db_query = getattr(backend, "db_query", None)
    if db_query is None:
        raise TypeError(
            "live resolution requires a dbsync-backed backend "
            f"(got {type(backend).__name__}, which has no db_query)",
        )
    return db_query(sql, args)


def utxo_assets(backend: AbstractBackend, tx_out_id: int) -> list[tuple[str, str, int]]:
    """The native assets on a tx_out, ordered canonically (policy, name).

    Mirrors the capture tools' asset ordering so a resolved :class:`Utxo` compares
    equal to a captured one.
    """
    rows = db_query_rows(
        backend,
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

    The reference script keeps its dbsync language (``ref_script_type``); an output
    with no inline datum carries its datum hash, if any, in ``datum_hash``. Filters to
    unspent (``consumed_by_tx_id IS NULL``) unless ``allow_spent`` is set, in which
    case an already-consumed (historical) output can still be resolved, since dbsync
    retains consumed rows. Raises :class:`ValueError` when no matching output is
    found.
    """
    spent_filter = "" if allow_spent else "AND o.consumed_by_tx_id IS NULL"
    rows = db_query_rows(
        backend,
        f"""SELECT o.id AS id,
                   o.value AS lovelace,
                   a.address AS address,
                   encode(d.bytes, 'hex') AS datum,
                   encode(o.data_hash, 'hex') AS datum_hash,
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
    # dbsync fills ``data_hash`` for inline datums too; keep it only when the output
    # carries no inline datum, so the two never both describe one output.
    datum_hash = None if row["datum"] else row.get("datum_hash")
    return utxo_from_dict(
        {
            "address": row["address"],
            "lovelace": int(row["lovelace"]),
            "assets": utxo_assets(backend, row["id"]),
            "datum": row["datum"],
            "datum_hash": datum_hash,
            "ref_script": row["ref_script"],
            "ref_script_type": row["ref_script_type"],
            "out_ref": [tx_hash, index],
        },
    )


def resolve_funding(
    backend: AbstractBackend,
    address: str,
    *,
    limit: int = 20,
) -> list[Utxo]:
    """Resolve the unspent UTxOs at ``address`` (most recent first, up to ``limit``)."""
    rows = db_query_rows(
        backend,
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
