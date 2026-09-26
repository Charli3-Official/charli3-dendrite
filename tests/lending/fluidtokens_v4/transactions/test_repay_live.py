"""Gated: dbsync resolves each captured repay and Ogmios evaluates it.

The captured loans are spent, so they resolve with ``allow_spent`` against the live
config; the funding and window come from the capture. Needs ``DBSYNC_*`` and
``OGMIOS_HOST``.
"""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import build_repay
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_dbsync

pytestmark = needs_dbsync


@pytest.fixture(scope="module")
def backend():  # noqa: ANN201
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


@pytest.mark.parametrize(
    ("name", "payments"),
    [
        ("repay_single", [20_000_004]),
        ("repay_multi", [59_999_957, 59_999_957, 379_999_723]),
    ],
)
def test_live_repay_pays_the_exact_debt(
    backend, name: str, payments: list[int]
) -> None:  # noqa: ANN001
    fix = fixture(name)
    capture = RepaySnapshot.from_capture(fix)
    snapshot = RepaySnapshot.from_backend(
        backend,
        loans=[p.out_ref for p in capture.positions],
        borrower_address=capture.positions[0].borrower_bond.address,
        funding=capture.funding,
        valid_from=capture.valid_from,
        valid_to=capture.valid_to,
        allow_spent=True,
    )
    assert [p.payment for p in snapshot.ordered_positions] == payments
    built = build(build_repay, snapshot, slot=fix["invalid_before"])
    utxos = fixture_utxos(fix) + [
        u for p in snapshot.positions for u in (p.loan, p.borrower_bond)
    ]
    scripts = [
        snapshot.config,
        snapshot.loan_spend_script_ref,
        snapshot.loan_policy_script_ref,
        snapshot.action_script_ref,
    ]
    # Loan spends, the loan NFT burn, loan dispatch + repay action withdraws.
    assert evaluate(built, utxos + scripts) == sorted(
        ["spend"] * len(payments) + ["mint", "withdraw", "withdraw"],
    )
