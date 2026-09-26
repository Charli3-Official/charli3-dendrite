"""Recast snapshots built from the captured repay's loan (no mainnet recast exists)."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import replace

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import RecastPosition
from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import RecastSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import reference_script


def recast_snapshot(
    edit: Callable[[LoanDatum], LoanDatum] = lambda d: d,
    *,
    amount_paid: int,
    receipts: bool = False,
) -> tuple[RecastSnapshot, list[Utxo], int]:
    """A recast of the captured loan with its datum edited.

    Returns the snapshot, every UTxO Ogmios needs, and the slot to build at.
    """
    fix = fixture("repay_single")
    repay = RepaySnapshot.from_capture(fix)
    (captured,) = repay.positions
    loan = replace(captured.loan, datum=edit(captured.loan_datum).to_cbor_hex())
    snapshot = RecastSnapshot(
        positions=[
            RecastPosition(
                loan=loan,
                borrower_bond=captured.borrower_bond,
                amount_paid=amount_paid,
            ),
        ],
        funding=repay.funding,
        config=repay.config,
        loan_spend_script_ref=repay.loan_spend_script_ref,
        loan_policy_script_ref=repay.loan_policy_script_ref,
        action_script_ref=reference_script("recast_action"),
        valid_from=repay.valid_from,
        valid_to=repay.valid_to,
        asset_manager_policy_script_ref=(
            reference_script("repayment_policy") if receipts else None
        ),
    )
    utxos = [u for u in fixture_utxos(fix) if u.out_ref != loan.out_ref]
    utxos += [loan, snapshot.action_script_ref]
    if snapshot.asset_manager_policy_script_ref is not None:
        utxos.append(snapshot.asset_manager_policy_script_ref)
    return snapshot, utxos, fix["invalid_before"]


def recast_batch_snapshot(
    amounts: Sequence[int],
) -> tuple[RecastSnapshot, list[Utxo], int]:
    """A recast of the three captured repay_multi loans, paying ``amounts`` in order.

    Returns the snapshot, every UTxO Ogmios needs, and the slot to build at.
    """
    fix = fixture("repay_multi")
    repay = RepaySnapshot.from_capture(fix)
    positions = [
        RecastPosition(
            loan=p.loan,
            borrower_bond=p.borrower_bond,
            amount_paid=amount,
        )
        for p, amount in zip(repay.ordered_positions, amounts)
    ]
    snapshot = RecastSnapshot(
        positions=positions,
        funding=repay.funding,
        config=repay.config,
        loan_spend_script_ref=repay.loan_spend_script_ref,
        loan_policy_script_ref=repay.loan_policy_script_ref,
        action_script_ref=reference_script("recast_action"),
        valid_from=repay.valid_from,
        valid_to=repay.valid_to,
    )
    utxos = [*fixture_utxos(fix), snapshot.action_script_ref]
    return snapshot, utxos, fix["invalid_before"]
