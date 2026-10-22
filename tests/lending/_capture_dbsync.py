"""Dev-only dbsync helpers shared by the lending fixture capture scripts.

Every query is a SELECT. ``record_sql`` is the base output select the scripts append
their ``where`` clause to; ``record_entry`` turns one of its rows into the fixture
entry ``{out_ref, address, datum_cbor, assets}``, ``assets`` in the charli3
``Assets`` shape (``{"lovelace": <int>, "<policyhex><namehex>": <int>, ...}``).
"""

from __future__ import annotations

from typing import Any

_COLUMNS = """encode(t.hash, 'hex'), o.index, a.address, o.value,
           encode(d.bytes, 'hex'), o.id"""
# Slot, block index and block time of the creating transaction, then the slot of
# the spending one (null while unspent).
_CHAIN_COLUMNS = """, b.slot_no, t.block_index,
           extract(epoch from b.time)::bigint, sb.slot_no"""


def record_sql(*, chain: bool = False) -> str:
    """The base output select over ``o`` (tx_out), ``t`` (tx), ``a``, ``d`` (datum).

    Columns: tx hash hex, output index, address, lovelace, inline datum hex, tx_out
    id. ``chain`` appends the creating slot, block index and block time (POSIX
    seconds) and the spending slot, joining ``b`` (block), ``st`` and ``sb``.
    """
    joins = [
        "join tx t on t.id = o.tx_id",
        "join address a on a.id = o.address_id",
        "left join datum d on d.id = o.inline_datum_id",
    ]
    columns = _COLUMNS
    if chain:
        columns += _CHAIN_COLUMNS
        joins.insert(1, "join block b on b.id = t.block_id")
        joins += [
            "left join tx st on st.id = o.consumed_by_tx_id",
            "left join block sb on sb.id = st.block_id",
        ]
    return (
        "\n    select "
        + columns
        + "\n    from tx_out o\n"
        + "".join(f"    {join}\n" for join in joins)
    )


def output_assets(cur: Any, tx_out_id: int, lovelace: int) -> dict[str, int]:
    """The output's value: lovelace first, then every token unit in unit order."""
    assets: dict[str, int] = {"lovelace": int(lovelace)}
    cur.execute(
        """select encode(ma.policy, 'hex') || encode(ma.name, 'hex'), mto.quantity
           from ma_tx_out mto
           join multi_asset ma on ma.id = mto.ident
           where mto.tx_out_id = %s
           order by 1""",
        (tx_out_id,),
    )
    for unit, quantity in cur.fetchall():
        assets[unit] = int(quantity)
    return assets


def record_entry(cur: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    """The fixture entry for a ``record_sql`` row (its first six columns)."""
    tx_hash, ix, address, lovelace, datum_hex, tx_out_id = row[:6]
    return {
        "out_ref": f"{tx_hash}#{ix}",
        "address": address,
        "datum_cbor": datum_hex or "",
        "assets": output_assets(cur, tx_out_id, lovelace),
    }
