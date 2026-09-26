"""FluidTokens V4 repay: one installment, or the whole debt, of one or several loans.

Each repaid loan pays its lender at the asset manager, owned by the loan's lender
bond. A repay that closes the loan burns the loan NFT and frees the collateral to the
caller's change; any other repay continues the loan with one more installment repaid
and its value unchanged. A loan that issues repayment receipts also mints one into the
lender's payment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanRepaymentData
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    LOAN_ACTION_VALIDITY_SLOTS,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import ledger_order
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
    repaid_loan_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    repayment_amount,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    repayment_closes_loan,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    ActionTypeRepay,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolFalse
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanRepayActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import RepayData
from charli3_dendrite.lending.units import constr
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

# The asset-manager action tag of an installment repayment.
INSTALLMENT_REPAYMENT = b"installment_repayment"

_BOOL_TRUE = 1


@dataclass
class RepayPosition(LoanPosition):
    """A loan to repay: what it pays and whether the repayment is final.

    ``payment`` is in principal units. ``is_final`` asks a perpetual loan to close;
    an installment loan closes on its last installment regardless. ``lender_lovelace``
    overrides the ADA the lender's payment carries.
    """

    payment: int
    is_final: bool
    lender_lovelace: int | None = None

    @property
    def closes(self) -> bool:
        """True if this repay closes the loan."""
        return repayment_closes_loan(self.loan_datum, is_final=self.is_final)


@dataclass
class RepaySnapshot(LoanActionSnapshot):
    """Everything a repay of one or several loans needs, resolved from chain."""

    positions: Sequence[RepayPosition]
    asset_manager_policy_script_ref: Utxo | None = None

    @classmethod
    def from_capture(cls, fix: dict) -> RepaySnapshot:
        """Rebuild the snapshot of a captured mainnet repay (for byte-exact replay)."""
        inputs = [utxo_from_dict(u) for u in fix["inputs"]]
        outputs = [utxo_from_dict(u) for u in fix["outputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        action = next(
            LoanRepayActionWithdrawRedeemer.from_cbor(r["cbor"])
            for r in fix["redeemers"]
            if r["script_hash"] == c.LOAN_REPAY_ACTION_SKH
        )
        loans = ledger_order(
            u for u in inputs if u.holds_policy(c.LOAN_POLICY) and u.datum
        )
        payments = [
            u
            for u in outputs
            if Address.decode(u.address).payment_part.payload.hex()
            == c.ASSET_MANAGER_SPEND_SKH
        ]
        positions = []
        for loan, data, paid in zip(loans, action.actions_for_each_input, payments):
            bond = next(
                u for u in inputs if u.holds(c.BORROWER_BOND_POLICY, data.loan_id.hex())
            )
            position = RepayPosition(
                loan=loan,
                borrower_bond=bond,
                payment=0,
                is_final=constr(data.is_final_repayment)[0] == _BOOL_TRUE,
                lender_lovelace=paid.lovelace,
            )
            unit = position.loan_datum.principal_asset.unit()
            position.payment = (
                paid.lovelace
                if unit == "lovelace"
                else next(q for p, n, q in paid.assets if p + n == unit)
            )
            positions.append(position)
        used = {u.out_ref for p in positions for u in (p.loan, p.borrower_bond)}
        try:
            receipt_script: Utxo | None = script_ref_by_hash(refs, c.REPAYMENT_POLICY)
        except ValueError:
            receipt_script = None
        return cls(
            positions=positions,
            funding=[u for u in inputs if u.out_ref not in used],
            config=next(
                u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
            ),
            loan_spend_script_ref=script_ref_by_hash(refs, c.LOAN_SPEND_SKH),
            loan_policy_script_ref=script_ref_by_hash(refs, c.LOAN_POLICY),
            action_script_ref=script_ref_by_hash(refs, c.LOAN_REPAY_ACTION_SKH),
            valid_from=fix["invalid_before"],
            valid_to=fix["invalid_hereafter"],
            asset_manager_policy_script_ref=receipt_script,
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loans: Sequence[tuple[str, int]],
        borrower_address: str,
        is_final: bool = True,
        funding: Sequence[Utxo] | None = None,
        valid_from: int | None = None,
        valid_to: int | None = None,
        allow_spent: bool = False,
    ) -> RepaySnapshot:
        """Resolve a repay of each loan in ``loans`` (out-refs) from the backend.

        Every loan pays the least its lender accepts at the window's upper bound: the
        whole debt when ``is_final`` (or the loan has no installments left to choose),
        else the next installment. The borrower bonds must sit at
        ``borrower_address``. The window defaults to
        :data:`~.common.LOAN_ACTION_VALIDITY_SLOTS` slots from the tip.
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

        context = resolve_loan_action(
            backend,
            action="repay",
            loan_out_refs=loans,
            borrower_address=borrower_address,
            funding=funding,
            allow_spent=allow_spent,
        )
        window = default_window(
            backend,
            valid_from=valid_from,
            valid_to=valid_to,
            slots=LOAN_ACTION_VALIDITY_SLOTS,
        )
        positions = [
            RepayPosition(
                loan=p.loan,
                borrower_bond=p.borrower_bond,
                payment=repayment_amount(
                    p.loan_datum,
                    valid_to_ms=slot_to_posix_ms(window[1]),
                    is_final=is_final,
                ),
                is_final=is_final,
            )
            for p in context.positions
        ]
        return cls(
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


def build_repay(tx_builder: TransactionBuilder, *, snapshot: RepaySnapshot) -> None:
    """Add a repay of every position of ``snapshot`` to ``tx_builder``.

    Loans that stay open must sort (by out-ref) before loans the repay closes: the
    repay action reads the i-th loan's continuing output as the i-th output to the loan
    script. A loan that stays open keeps its value, so a loan whose ADA no longer
    covers its continuing output is refused. The caller balances, signs and submits;
    the freed collateral of a closed loan lands in the caller's change.

    Everything else the caller wants among the transaction's inputs, reference
    inputs, mints and withdrawals must be added before this call, which fills their
    indexes last; outputs may be added after, except to the loan and asset-manager
    scripts. Discard the builder if this raises.
    """
    positions: list[RepayPosition] = snapshot.ordered_positions  # type: ignore[assignment]
    closes = [p.closes for p in positions]
    require_open_before_closing(closes, "repay")
    continuing = [_continuing_loan(p) for p in positions if not p.closes]

    repay_data = [
        RepayData(
            borrower_bond_output_index=0,
            loan_id=p.loan_id,
            is_final_repayment=BoolTrue() if p.is_final else BoolFalse(),
        )
        for p in positions
    ]
    action = LoanRepayActionWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=repay_data,
    )
    dispatch = add_loan_action(
        tx_builder,
        snapshot=snapshot,
        action_type=ActionTypeRepay(),
        action=action,
    )
    burn = add_loan_burn(
        tx_builder,
        snapshot=snapshot,
        closing=[p for p in positions if p.closes],
    )
    receipts = add_receipt_mint(
        tx_builder,
        script_ref=snapshot.asset_manager_policy_script_ref,
        positions=positions,
    )

    for loan in continuing:
        tx_builder.add_output(loan)
    for position in positions:
        datum = position.loan_datum
        tx_builder.add_output(
            asset_manager_output(
                position,
                amount=position.payment,
                action=INSTALLMENT_REPAYMENT,
                data=LoanRepaymentData(
                    loan_id=position.loan_id,
                    principal_amount=datum.principal_amount,
                    interest_rate=datum.interest_rate,
                    repaid_installments=datum.repaid_installments + 1,
                    total_installments=datum.total_installments,
                    repayment_mode=datum.repayment_mode,
                ),
                slot=snapshot.valid_from,
                lovelace=position.lender_lovelace,
            ),
        )
    bond_indexes = add_bond_outputs(tx_builder, positions)
    for data, index in zip(repay_data, bond_indexes):
        data.borrower_bond_output_index = index
    finish_loan_action(
        tx_builder,
        snapshot=snapshot,
        dispatch=dispatch,
        action=action,
        burn=burn,
        receipts=receipts,
    )


def _continuing_loan(position: RepayPosition) -> TransactionOutput:
    """The loan after one installment: same address and value, one more repaid."""
    return continuing_loan_output(position, repaid_loan_datum(position.loan_datum))
