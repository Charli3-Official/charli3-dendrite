"""Gated live sanity gate for the perpetual debt + health math (FLUID_E2E=1).

Snapshots the live mainnet deployment and checks the perpetual interest / health
model against real on-chain loans. This is the M1 read-only gate: it confirms the
`math.py` perpetual formula produces self-consistent, monotonic, and plausibly
bounded debt for every live perpetual loan, and that health/liquidation behave
safely when collateral is unpriced.

Note: an exact cross-check against the FluidTokens app-displayed debt is not
performed here (no programmatic app access); the assertions below validate the
formula's internal consistency and bounds against live datums. Skipped unless
`FLUID_E2E=1` (needs reachable dbsync creds from the environment / `.env`).
"""

import os
import time
from decimal import Decimal

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.environ.get("FLUID_E2E") != "1",
    reason="requires live dbsync (FLUID_E2E=1)",
)

_MS_PER_YEAR = 365 * 24 * 3_600_000


def _backend():
    load_dotenv()
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _perpetual_loans(active):
    from charli3_dendrite.lending.fluidtokens.state import _REPAYMENT_PERPETUAL
    from charli3_dendrite.lending.units import constr

    out = []
    for ln in active:
        alt, _ = constr(ln.loan_datum.repayment_mode)
        if alt == _REPAYMENT_PERPETUAL:
            out.append(ln)
    return out


def test_live_perpetual_debt_and_health_sanity():
    from charli3_dendrite.lending.fluidtokens.loader import snapshot
    from charli3_dendrite.lending.fluidtokens.math import perpetual_outstanding_debt
    from charli3_dendrite.lending.fluidtokens.state import _REPAYMENT_PERPETUAL
    from charli3_dendrite.lending.oracles.models import PriceMap
    from charli3_dendrite.lending.units import constr

    now_ms = int(time.time() * 1000)
    book = snapshot(_backend(), now_ms=now_ms)

    active = book.active_loans()
    assert active, "no active loans parsed on-chain"

    perpetual = _perpetual_loans(active)
    assert perpetual, "no perpetual loans found on-chain"

    # Every perpetual loan: debt never below principal, and (with no prices) it is
    # not flagged liquidatable — an oracle outage must not produce false positives.
    empty = PriceMap()
    for ln in perpetual:
        principal = ln.loan_datum.principal_amount
        assert ln.current_debt() >= principal
        assert ln.is_liquidatable(empty) is False

    # Largest loan: interest must accrue strictly over a one-year horizon, and the
    # implied annualized growth must be plausibly bounded (< 100%/yr).
    biggest = max(perpetual, key=lambda ln: ln.loan_datum.principal_amount)
    ld = biggest.loan_datum
    mode_alt, mode_fields = constr(ld.repayment_mode)
    assert mode_alt == _REPAYMENT_PERPETUAL
    apy_coef = int(mode_fields[0])

    debt_now = biggest.current_debt()
    debt_future = perpetual_outstanding_debt(
        principal=ld.principal_amount,
        interest_rate=ld.interest_rate,
        apy_coef=apy_coef,
        lend_date_ms=ld.lend_date,
        now_ms=now_ms + _MS_PER_YEAR,
    )
    assert debt_future > debt_now, "perpetual debt must accrue over time"

    annual_growth = Decimal(debt_future - ld.principal_amount) / Decimal(
        ld.principal_amount
    )
    assert (
        Decimal(0) < annual_growth < Decimal(1)
    ), f"implied annual growth {annual_growth} out of sane (0, 1) band"

    # Health factor is computable and non-negative for the largest loan.
    hf = biggest.health_factor(empty)
    assert hf >= 0
