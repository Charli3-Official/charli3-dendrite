"""Gated: V4 repays evaluate on Ogmios against mainnet (OGMIOS_HOST).

The captured repays are all final repays of perpetual loans without installments, as
is every live V4 loan. The other repayment modes run the captured loan with a changed
datum: Ogmios reads the spent loan from ``additionalUtxo``, so the scripts see the
changed terms.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from charli3_dendrite.lending.fluidtokens.datums import InterestOnRemainingPrincipal
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    repayment_amount,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import build_repay
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios
from tests.lending.fluidtokens_v4.transactions.replay import reference_script

pytestmark = needs_ogmios


@pytest.mark.parametrize(("name", "loans"), [("repay_single", 1), ("repay_multi", 3)])
def test_captured_repay_evaluates(name: str, loans: int) -> None:
    fix = fixture(name)
    built = build(
        build_repay, RepaySnapshot.from_capture(fix), slot=fix["invalid_before"]
    )
    assert evaluate(built, fixture_utxos(fix)) == sorted(
        ["spend"] * loans + ["mint", "withdraw", "withdraw"],
    )


def _amortized(datum: LoanDatum) -> LoanDatum:
    return replace(
        datum,
        repayment_mode=InterestOnRemainingPrincipal(max_possible_recasts=3),
        total_installments=6,
        installment_period=720,
        interest_rate=1200,
    )


def _repay(
    edit: Callable[[LoanDatum], LoanDatum],
    *,
    is_final: bool,
    receipts: bool = False,
    shortfall: int = 0,
) -> list[str]:
    """Repay the captured loan with its datum edited; the evaluated purposes."""
    fix = fixture("repay_single")
    snapshot = RepaySnapshot.from_capture(fix)
    (position,) = snapshot.positions
    position.loan = replace(
        position.loan,
        datum=edit(position.loan_datum).to_cbor_hex(),
    )
    position.is_final = is_final
    position.payment = (
        repayment_amount(
            position.loan_datum,
            valid_to_ms=slot_to_posix_ms(snapshot.valid_to),
            is_final=is_final,
        )
        - shortfall
    )
    position.lender_lovelace = position.payment if shortfall else None
    utxos = [u for u in fixture_utxos(fix) if u.out_ref != position.out_ref]
    utxos.append(position.loan)
    if receipts:
        snapshot.asset_manager_policy_script_ref = reference_script("repayment_policy")
        utxos.append(snapshot.asset_manager_policy_script_ref)
    built = build(build_repay, snapshot, slot=fix["invalid_before"])
    return evaluate(built, utxos)


_CONTINUES = ["spend", "withdraw", "withdraw"]
_CLOSES = ["mint", "spend", "withdraw", "withdraw"]


@pytest.mark.parametrize(
    ("edit", "is_final", "receipts", "purposes"),
    [
        (lambda d: replace(d, installment_period=720), False, False, _CONTINUES),
        (lambda d: replace(d, installment_period=720), True, False, _CLOSES),
        (
            lambda d: replace(d, repayment_receipts=BoolTrue()),
            True,
            True,
            ["mint", *_CLOSES],
        ),
        (
            lambda d: replace(d, installment_period=720, repayment_receipts=BoolTrue()),
            False,
            True,
            ["mint", *_CONTINUES],
        ),
        (_amortized, False, False, _CONTINUES),
        (
            lambda d: replace(_amortized(d), repaid_installments=5),
            False,
            False,
            _CLOSES,
        ),
    ],
    ids=[
        "perpetual-installment",
        "perpetual-final-with-installments",
        "final-with-receipt",
        "installment-with-receipt",
        "amortized-first",
        "amortized-last",
    ],
)
def test_repayment_modes_evaluate(
    edit: Callable[[LoanDatum], LoanDatum],
    is_final: bool,  # noqa: FBT001
    receipts: bool,  # noqa: FBT001
    purposes: list[str],
) -> None:
    assert _repay(edit, is_final=is_final, receipts=receipts) == sorted(purposes)


@pytest.mark.parametrize(
    ("edit", "is_final"),
    [(lambda d: d, True), (_amortized, False)],
    ids=["final", "amortized-installment"],
)
def test_paying_one_unit_less_fails(
    edit: Callable[[LoanDatum], LoanDatum],
    is_final: bool,  # noqa: FBT001
) -> None:
    with pytest.raises(AssertionError, match="ogmios evaluate error"):
        _repay(edit, is_final=is_final, shortfall=1)


def test_loans_sharing_one_bond_utxo_evaluate() -> None:
    fix = fixture("repay_multi")
    snapshot = RepaySnapshot.from_capture(fix)
    first = snapshot.positions[0].borrower_bond
    shared = replace(
        first,
        out_ref=("bb" * 32, 0),
        assets=[a for p in snapshot.positions for a in p.borrower_bond.assets],
        datum=None,
    )
    for position in snapshot.positions:
        position.borrower_bond = shared
    built = build(build_repay, snapshot, slot=fix["invalid_before"])
    assert evaluate(built, [*fixture_utxos(fix), shared]) == sorted(
        ["spend"] * 3 + ["mint", "withdraw", "withdraw"],
    )
