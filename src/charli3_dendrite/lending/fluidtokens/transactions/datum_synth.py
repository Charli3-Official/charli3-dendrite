"""Synthesize the FluidTokens repayment-receipt datum from the loan being repaid.

On repay the lender is paid via a wallet output carrying a repayment receipt that
records the loan being closed. The receipt is fully derivable from the loan UTxO's
:class:`LoanDatum` + the loan's out-ref + the lender-bond policy, so it reproduces the
on-chain datum byte-exact (verified in test_datum_synth).
"""
from __future__ import annotations

from dataclasses import dataclass

from pycardano import Datum
from pycardano import PlutusData

from charli3_dendrite.lending.fluidtokens.datums import LoanDatum

# The receipt's tag bytestring identifying an installment repayment.
INSTALLMENT_REPAYMENT_TAG = b"installment_repayment"


@dataclass
class TxOutRef(PlutusData):
    """A transaction output reference == Constr0([tx_id_bytes, index])."""

    CONSTR_ID = 0
    tx_id: bytes
    index: int


@dataclass
class RepaymentInfo(PlutusData):
    """The loan terms recorded on the receipt == Constr0([...]).

    Mirrors the loan datum's identity/terms: the loan id, principal amount, interest
    rate, recasts done, installments repaid, and the loan's repayment mode (carried
    through verbatim from the loan datum).
    """

    CONSTR_ID = 0
    loan_id: bytes
    principal_amount: int
    interest_rate: int
    done_recasts: int
    repaid_installments: int
    repayment_mode: Datum


@dataclass
class LenderBondRef(PlutusData):
    """Identifies the lender-bond the receipt credits == Constr0([policy, loan_id])."""

    CONSTR_ID = 0
    lender_bond_policy: bytes
    loan_id: bytes


@dataclass
class RepaymentReceiptDatum(PlutusData):
    """The repayment-receipt datum on the lender output == Constr0([...])."""

    CONSTR_ID = 0
    loan_out_ref: TxOutRef
    tag: bytes
    info: RepaymentInfo
    lender_bond: LenderBondRef


def synth_repayment_receipt(
    *,
    loan_datum: LoanDatum,
    loan_out_ref: tuple[str, int],
    loan_id: bytes,
    lender_bond_policy: str,
) -> RepaymentReceiptDatum:
    """Build the repayment-receipt datum for the lender output of a (full) repay.

    All fields derive from the loan UTxO being spent: its out-ref pins the receipt to
    the loan, and its :class:`LoanDatum` supplies the recorded terms. The repayment mode
    is carried through verbatim (a raw Plutus value) so the receipt reproduces the loan
    datum's mode byte-exact.
    """
    return RepaymentReceiptDatum(
        loan_out_ref=TxOutRef(
            tx_id=bytes.fromhex(loan_out_ref[0]),
            index=loan_out_ref[1],
        ),
        tag=INSTALLMENT_REPAYMENT_TAG,
        info=RepaymentInfo(
            loan_id=loan_id,
            principal_amount=loan_datum.principal_amount,
            interest_rate=loan_datum.interest_rate,
            done_recasts=loan_datum.done_recasts,
            repaid_installments=loan_datum.repaid_installments,
            repayment_mode=loan_datum.repayment_mode,
        ),
        lender_bond=LenderBondRef(
            lender_bond_policy=bytes.fromhex(lender_bond_policy),
            loan_id=loan_id,
        ),
    )
