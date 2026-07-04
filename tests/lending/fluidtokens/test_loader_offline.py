"""Pure `build_book` over real captured FluidTokens UTxOs (no network).

Exercises the deterministic stitching core: build pool/loan state from the
captured `entities.json`, attach market + loan context, force the loan underwater
with an aggregated collateral price, and confirm it lands in the book's
liquidatable set. A second case checks the empty-input identity.
"""

import json
import time
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.base import LendingBook
from charli3_dendrite.lending.fluidtokens.loader import build_book
from charli3_dendrite.lending.fluidtokens.market import FluidMarket
from charli3_dendrite.lending.fluidtokens.state import FluidLoanState
from charli3_dendrite.lending.fluidtokens.state import FluidPoolState
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())

_MS_PER_YEAR = 365 * 24 * 3_600_000


def _assets(entity: str) -> Assets:
    return Assets(root=dict(FIX[entity]["assets"]))


def _pool_state() -> FluidPoolState:
    rec = FIX["pool"]
    pool = FluidPoolState(
        address=rec["address"],
        assets=_assets("pool"),
        block_time=int(time.time() * 1000),
        block_index=0,
        plutus_v2=True,
        datum_cbor=rec["datum_cbor"],
        datum_hash="",
        tx_index=0,
        tx_hash="",
    )
    pool.attach_market(FluidMarket.from_pool_datum(pool.pool_datum))
    return pool


def _loan_state() -> FluidLoanState:
    rec = FIX["loan"]
    loan = FluidLoanState(
        address=rec["address"],
        assets=_assets("loan"),
        block_time=int(time.time() * 1000),
        block_index=0,
        plutus_v2=True,
        datum_cbor=rec["datum_cbor"],
        datum_hash="",
        tx_index=0,
        tx_hash="",
    )
    pool = _pool_state()
    now_ms = loan.loan_datum.lend_date + _MS_PER_YEAR
    loan.attach_context(pool=pool, market=pool.market, now_ms=now_ms)
    return loan


def test_build_book_stitches_and_flags_underwater_loan():
    loan = _loan_state()
    pool = loan._pool

    # An aggregated collateral price far below par makes the position underwater:
    # collateral_value << debt, so health factor <= 1.
    pm = PriceMap()
    pm.add(
        OraclePrice(
            token=loan._collateral_unit(),
            quote="lovelace",
            num=1,
            denom=1000,
            source=OracleSource.FLUID_AGGREGATED,
        )
    )

    book = build_book(pools=[pool], loans=[loan], prices=pm, now_ms=loan._now_ms)

    assert loan in book.active_loans()
    assert book.by_pool(loan.pool_id) == [loan]
    assert loan in book.liquidatable_loans()


def test_build_book_empty_inputs():
    book = build_book(pools=[], loans=[], prices=PriceMap(), now_ms=0)
    assert isinstance(book, LendingBook)
    assert book.active_loans() == []
