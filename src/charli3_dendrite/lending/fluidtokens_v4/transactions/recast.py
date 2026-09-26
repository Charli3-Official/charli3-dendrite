"""FluidTokens V4 recast: pay down principal and restart one or several loans' terms.

Each recast pays its lender at the asset manager, like a repay, and continues the loan
with the recast principal and one more recast done; an amortized loan also restarts its
installments. A payment that leaves no principal closes the loan: its NFT is burned and
the collateral lands in the caller's change. A payment above that payoff is refused,
since the contract would keep the surplus for the lender.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    LOAN_ACTION_VALIDITY_SLOTS,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    LoanActionSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    LoanPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    add_bond_outputs,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    add_loan_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    add_loan_burn,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    add_receipt_mint,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    asset_manager_output,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    continuing_loan_output,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    finish_loan_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    issues_receipts,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    require_open_before_closing,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    recast_loan_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    recast_principal,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    ActionTypeRecast,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanRecastActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import RecastData
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

# The asset-manager action tag of a recast payment.
RECAST = b"recast"


@dataclass
class RecastPosition(LoanPosition):
    """A loan to recast by paying ``amount_paid`` (principal units)."""

    amount_paid: int


@dataclass
class RecastSnapshot(LoanActionSnapshot):
    """Everything a recast of one or several loans needs, resolved from chain."""

    positions: Sequence[RecastPosition]
    asset_manager_policy_script_ref: Utxo | None = None

    def new_principal(self, position: RecastPosition) -> int:
        """The loan's principal after this recast (zero closes it).

        Raises ``ValueError`` for a payment above the payoff: the contract accepts it,
        closes the loan and keeps the surplus at the asset manager for the lender.
        """
        principal = recast_principal(
            position.loan_datum,
            amount_paid=position.amount_paid,
            valid_to_ms=slot_to_posix_ms(self.valid_to),
        )
        if principal < 0:
            raise ValueError(
                f"loan {position.out_ref} is paid off by "
                f"{position.amount_paid + principal}; paying {position.amount_paid} "
                "would give the surplus to the lender",
            )
        return principal

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        recasts: Sequence[tuple[tuple[str, int], int]],
        borrower_address: str,
        funding: Sequence[Utxo] | None = None,
        valid_from: int | None = None,
        valid_to: int | None = None,
        allow_spent: bool = False,
    ) -> RecastSnapshot:
        """Resolve a recast for each ``(loan out-ref, amount paid)``.

        Raises ``ValueError`` while the recast action's reward account is unregistered
        (the ledger rejects its withdrawal, so no recast can be submitted), for a
        recast the contract rejects, and for a payment above a loan's payoff. The
        window defaults to :data:`~.common.LOAN_ACTION_VALIDITY_SLOTS` slots from the
        tip.
        """
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            default_window,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_loan_action,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_script,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            reward_account_registered,
        )

        context = resolve_loan_action(
            backend,
            action="recast",
            loan_out_refs=[out_ref for out_ref, _ in recasts],
            borrower_address=borrower_address,
            funding=funding,
            allow_spent=allow_spent,
        )
        action_hash = context.scripts.loan_recast_action_script_hash.hex()
        if not reward_account_registered(backend, action_hash):
            raise ValueError(
                f"the recast action's reward account ({action_hash}) is not "
                "registered, so the ledger rejects every recast",
            )
        window = default_window(
            backend,
            valid_from=valid_from,
            valid_to=valid_to,
            slots=LOAN_ACTION_VALIDITY_SLOTS,
        )
        positions = [
            RecastPosition(
                loan=p.loan,
                borrower_bond=p.borrower_bond,
                amount_paid=amount,
            )
            for p, (_, amount) in zip(context.positions, recasts)
        ]
        snapshot = cls(
            positions=positions,
            funding=context.funding,
            config=context.config,
            loan_spend_script_ref=context.loan_spend_script_ref,
            loan_policy_script_ref=context.loan_policy_script_ref,
            action_script_ref=context.action_script_ref,
            valid_from=window[0],
            valid_to=window[1],
            asset_manager_policy_script_ref=(
                resolve_script(backend, context.scripts.repayment_policy_id)
                if issues_receipts(positions)
                else None
            ),
        )
        for position in positions:
            snapshot.new_principal(position)
        return snapshot


def build_recast(tx_builder: TransactionBuilder, *, snapshot: RecastSnapshot) -> None:
    """Add a recast of every position of ``snapshot`` to ``tx_builder``.

    Loans that stay open must sort (by out-ref) before loans the recast closes, as for
    a repay. A payment above a loan's payoff is refused, and so is a loan that stays
    open whose ADA no longer covers its continuing output (it must keep its value).
    The caller balances, signs and submits.

    Everything else the caller wants among the transaction's inputs, reference
    inputs, mints and withdrawals must be added before this call, which fills their
    indexes last; outputs may be added after, except to the loan and asset-manager
    scripts. Discard the builder if this raises.
    """
    positions: list[RecastPosition] = snapshot.ordered_positions  # type: ignore[assignment]
    principals = [snapshot.new_principal(p) for p in positions]
    closes = [principal <= 0 for principal in principals]
    require_open_before_closing(closes, "recast")
    continuing = [
        _continuing_loan(position, principal)
        for position, principal, closed in zip(positions, principals, closes)
        if not closed
    ]

    recast_data = [
        RecastData(
            borrower_bond_output_index=0,
            amount_paid=p.amount_paid,
            loan_id=p.loan_id,
        )
        for p in positions
    ]
    action = LoanRecastActionWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=recast_data,
    )
    dispatch = add_loan_action(
        tx_builder,
        snapshot=snapshot,
        action_type=ActionTypeRecast(),
        action=action,
    )
    burn = add_loan_burn(
        tx_builder,
        snapshot=snapshot,
        closing=[p for p, closed in zip(positions, closes) if closed],
    )
    receipts = add_receipt_mint(
        tx_builder,
        script_ref=snapshot.asset_manager_policy_script_ref,
        positions=positions,
    )

    for loan in continuing:
        tx_builder.add_output(loan)
    for position in positions:
        tx_builder.add_output(
            asset_manager_output(
                position,
                amount=position.amount_paid,
                action=RECAST,
                data=position.loan_id,
                slot=snapshot.valid_from,
            ),
        )
    bond_indexes = add_bond_outputs(tx_builder, positions)
    for data, index in zip(recast_data, bond_indexes):
        data.borrower_bond_output_index = index
    finish_loan_action(
        tx_builder,
        snapshot=snapshot,
        dispatch=dispatch,
        action=action,
        burn=burn,
        receipts=receipts,
    )


def _continuing_loan(position: RecastPosition, principal: int) -> TransactionOutput:
    """The recast loan: same address and value, recast datum."""
    return continuing_loan_output(
        position,
        recast_loan_datum(position.loan_datum, new_principal=principal),
    )
