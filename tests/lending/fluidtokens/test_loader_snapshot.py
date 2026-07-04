"""End-to-end `snapshot` over a fake backend serving real mainnet UTxOs.

Exercises discover -> fetch -> parse -> stitch -> build_book without a network by
replaying the captured FluidTokens pool, loan, and request UTxOs. The fake backend
dispatches `get_pool_utxos(addresses=...)` by matching the requested address to the
captured entity addresses by PAYMENT CREDENTIAL -- mirroring the real dbsync backend,
which matches on payment credential and ignores the stake part.
"""

import json
from pathlib import Path

from pycardano import Address

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.fluidtokens.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens.loader import snapshot

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())


def _info(entity: str) -> PoolStateInfo:
    rec = FIX[entity]
    return PoolStateInfo(
        address=rec["address"],
        tx_hash="ab",
        tx_index=0,
        block_time=1_781_900_000,
        block_index=1,
        block_hash="cd",
        datum_hash="00",
        datum_cbor=rec["datum_cbor"],
        assets=Assets(root=dict(rec["assets"])),
        plutus_v2=True,
    )


def _payment_cred(address: str) -> str:
    return Address.decode(address).payment_part.payload.hex()


class _FakeBackend:
    """Dispatches get_pool_utxos by matching the requested address's payment cred."""

    def __init__(self) -> None:
        self._by_cred = {
            _payment_cred(FIX[e]["address"]): _info(e)
            for e in ("pool", "loan", "request")
        }

    def get_pool_utxos(
        self, addresses, assets=None, limit=1000, page=0, historical=True
    ):
        rows = [
            self._by_cred[cred]
            for a in addresses
            if (cred := _payment_cred(a)) in self._by_cred
        ]
        return PoolStateList(root=rows)


def test_snapshot_builds_book_from_real_utxos():
    loan_datum = LoanDatum.from_cbor(FIX["loan"]["datum_cbor"])
    now_ms = loan_datum.lend_date + 365 * 24 * 3_600_000

    book = snapshot(_FakeBackend(), now_ms=now_ms)

    # One pool discovered and paired with its market.
    assert book.pool is not None
    assert book.pool.market.principal_unit == "lovelace"

    # The captured loan stitches to the captured pool via origin_id <-> pool name.
    active = book.active_loans()
    assert len(active) == 1
    loan = active[0]
    assert loan.pool_id == book.pool.pool_id
    # Perpetual debt accrues at/above the loan datum principal.
    assert loan.current_debt() >= loan_datum.principal_amount
