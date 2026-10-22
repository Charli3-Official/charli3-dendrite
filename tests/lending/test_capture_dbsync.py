"""The dbsync helpers shared by the lending fixture capture scripts (no database)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from tests.lending._capture_dbsync import output_assets
from tests.lending._capture_dbsync import record_entry
from tests.lending._capture_dbsync import record_sql

LENDING = Path(__file__).parent
SCRIPTS = (
    LENDING / "fluidtokens_v4" / "fixtures" / "_capture_entities.py",
    LENDING / "liqwid" / "fixtures" / "_capture_records.py",
)
SHARED = {"record_entry", "record_sql"}

PLAIN_SQL = """
    select encode(t.hash, 'hex'), o.index, a.address, o.value,
           encode(d.bytes, 'hex'), o.id
    from tx_out o
    join tx t on t.id = o.tx_id
    join address a on a.id = o.address_id
    left join datum d on d.id = o.inline_datum_id
"""
CHAIN_SQL = """
    select encode(t.hash, 'hex'), o.index, a.address, o.value,
           encode(d.bytes, 'hex'), o.id, b.slot_no, t.block_index,
           extract(epoch from b.time)::bigint, sb.slot_no
    from tx_out o
    join tx t on t.id = o.tx_id
    join block b on b.id = t.block_id
    join address a on a.id = o.address_id
    left join datum d on d.id = o.inline_datum_id
    left join tx st on st.id = o.consumed_by_tx_id
    left join block sb on sb.id = st.block_id
"""
POLICY = "aa" * 28


class FakeCursor:
    """Answers every query with ``rows`` and records what was executed."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, args: tuple[Any, ...]) -> None:
        self.executed.append((sql, args))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def test_record_sql_is_the_base_select_with_optional_chain_columns():
    assert record_sql() == PLAIN_SQL
    assert record_sql(chain=True) == CHAIN_SQL


def test_output_assets_puts_lovelace_first_then_the_units_in_query_order():
    cur = FakeCursor([(POLICY + "01", 5), (POLICY + "02", 7)])
    assert list(output_assets(cur, 42, 2_000_000).items()) == [
        ("lovelace", 2_000_000),
        (POLICY + "01", 5),
        (POLICY + "02", 7),
    ]
    ((sql, args),) = cur.executed
    assert args == (42,)
    assert "where mto.tx_out_id = %s" in sql
    assert sql.rstrip().endswith("order by 1")


@pytest.mark.parametrize("datum", ["d87980", None])
def test_record_entry_reads_the_base_columns(datum):
    cur = FakeCursor([(POLICY + "01", 1)])
    row = ("ab" * 32, 3, "addr1example", 1_500_000, datum, 99, 123, 4)
    assert record_entry(cur, row) == {
        "out_ref": "ab" * 32 + "#3",
        "address": "addr1example",
        "datum_cbor": datum or "",
        "assets": {"lovelace": 1_500_000, POLICY + "01": 1},
    }
    assert cur.executed[0][1] == (99,)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.parent.parent.name)
def test_capture_scripts_import_the_shared_helpers(script):
    tree = ast.parse(script.read_text())
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "tests.lending._capture_dbsync"
        for alias in node.names
    }
    defined = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    } | {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert imported == SHARED
    assert not defined & {"_assets", "_RECORD_SQL"}
