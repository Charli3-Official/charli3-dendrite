import json
import time
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.constants import LOAN_POLICY
from charli3_dendrite.lending.fluidtokens.market import FluidMarket
from charli3_dendrite.lending.fluidtokens.state import FluidLoanState
from charli3_dendrite.lending.fluidtokens.state import FluidPoolState
from charli3_dendrite.lending.fluidtokens.state import FluidRequestState

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())

_MS_PER_YEAR = 365 * 24 * 3_600_000

# SNEK collateral unit held by the request UTxO (policy + "SNEK").
SNEK_UNIT = "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f534e454b"


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


def _request_state() -> FluidRequestState:
    rec = FIX["request"]
    return FluidRequestState(
        address=rec["address"],
        assets=_assets("request"),
        datum_cbor=rec["datum_cbor"],
    )


def test_pool_state():
    pool = _pool_state()
    assert pool.protocol() == "FluidTokens"
    assert pool.borrowable_unit == "lovelace"
    assert pool.pool_id
    # pool_id is stable across constructions of the same UTxO.
    assert pool.pool_id == _pool_state().pool_id
    assert pool.max_borrow_for_collateral_value(1_000_000_000) > 0


def test_loan_state():
    loan = _loan_state()
    assert loan.borrowed_unit == "lovelace"
    # Perpetual debt accrues at/above principal once any time elapses.
    assert loan.current_debt() >= 3_500_000_000
    assert loan.loan_id.startswith(LOAN_POLICY)


def test_request_state():
    req = _request_state()
    assert req.max_principal == 35_000_000_000
    assert req.collateral_unit == SNEK_UNIT
    assert req.collateral_unit != "lovelace"
