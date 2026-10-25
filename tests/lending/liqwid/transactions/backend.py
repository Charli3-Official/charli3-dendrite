"""An offline backend serving captured Liqwid UTxOs to the live resolvers.

:class:`FixtureBackend` answers the three backend calls ``resolve.py`` makes:

- ``get_pool_utxos``: the records whose payment credential matches one of the
  addresses (the stake part is ignored, as dbsync does), holding one of ``assets``
  when it is given; spent records only when ``historical`` is set; paged by
  ``limit``/``page``;
- ``db_query``: the out-ref, asset and wallet queries of
  :mod:`charli3_dendrite.lending.transactions.resolve`, answered from canned
  :class:`Utxo` s (a spent one only when the query does not filter spent outputs);
- ``last_block``: one block at ``tip_time`` (POSIX seconds).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pycardano import Address

from charli3_dendrite.dataclasses.models import BlockInfo
from charli3_dendrite.dataclasses.models import BlockList
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.transactions.utxos import Utxo
from tests.lending.liqwid.records import UTXO_KINDS
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of
from tests.lending.records_common import BLOCK_TIME

_UNSPENT_FILTER = "consumed_by_tx_id IS NULL"


def _credential(address: str) -> bytes:
    return Address.decode(address).payment_part.payload


def _out_ref(utxo: Utxo) -> tuple[str, int]:
    assert utxo.out_ref is not None
    return (utxo.out_ref[0], int(utxo.out_ref[1]))


class FixtureBackend:
    """Serves canned UTxO records, out-ref rows and a chain tip, offline."""

    def __init__(
        self,
        records: Iterable[PoolStateInfo] = (),
        utxos: Iterable[Utxo] = (),
        rows: Iterable[PoolStateInfo] = (),
        *,
        spent: Iterable[Utxo] = (),
        tip_time: int = BLOCK_TIME,
    ) -> None:
        """``records`` are unspent and ``rows`` spent UTxO records; ``utxos`` and
        ``spent`` the out-ref rows ``db_query`` resolves (unspent and spent).
        """
        self.records = list(records)
        self.rows = list(rows)
        self.tip_time = tip_time
        self.calls: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []
        self._utxos: list[tuple[Utxo, bool]] = [(u, False) for u in utxos]
        self._utxos += [(u, True) for u in spent]

    def get_pool_utxos(
        self,
        addresses: list[str],
        assets: list[str] | None = None,
        limit: int = 1000,
        page: int = 0,
        historical: bool = True,
    ) -> list[PoolStateInfo]:
        self.calls.append(
            {
                "addresses": tuple(addresses),
                "assets": None if assets is None else tuple(assets),
                "limit": limit,
                "page": page,
                "historical": historical,
            },
        )
        credentials = {_credential(address) for address in addresses}
        pool = self.records + (self.rows if historical else [])
        found = [
            info
            for info in pool
            if _credential(info.address) in credentials
            and (assets is None or any(unit in info.assets.root for unit in assets))
        ]
        return found[page * limit : (page + 1) * limit]

    def db_query(self, sql: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        self.queries.append(dict(args))
        live_only = _UNSPENT_FILTER in sql
        if "h" in args:
            for oid, (utxo, is_spent) in enumerate(self._utxos):
                if _out_ref(utxo) == (args["h"], int(args["i"])) and not (
                    is_spent and live_only
                ):
                    return [
                        {
                            "id": oid,
                            "lovelace": utxo.lovelace,
                            "address": utxo.address,
                            "datum": utxo.datum,
                            "datum_hash": utxo.datum_hash,
                            "ref_script": utxo.ref_script,
                            "ref_script_type": utxo.ref_script_type,
                        },
                    ]
            return []
        if "oid" in args:
            utxo = self._utxos[args["oid"]][0]
            return [
                {"policy": p, "name": n, "quantity": q}
                for p, n, q in sorted(utxo.assets)
            ]
        if "addr" in args:
            held = [
                utxo
                for utxo, is_spent in reversed(self._utxos)
                if utxo.address == args["addr"] and not is_spent
            ]
            return [
                {"tx_hash": _out_ref(u)[0], "idx": _out_ref(u)[1]}
                for u in held[: args["lim"]]
            ]
        raise AssertionError(f"unexpected query args {sorted(args)}")

    def last_block(self, last_n_blocks: int = 2) -> BlockList:
        return BlockList(
            root=[
                BlockInfo(
                    epoch_slot_no=0,
                    block_no=0,
                    tx_count=0,
                    block_time=self.tip_time,
                ),
            ],
        )


def live_records(*kinds: str) -> list[PoolStateInfo]:
    """The captured live UTxO records of ``kinds`` (every routed kind by default)."""
    return [
        record_info(rec)
        for kind in kinds or UTXO_KINDS
        for rec in records_of(kind, live=True)
    ]
