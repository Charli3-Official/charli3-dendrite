"""Synthesize the FluidTokens repayment-receipt datum from the loan being repaid.

On repay the lender is paid via a wallet output carrying a repayment receipt that
records the loan being closed. The receipt is fully derivable from the loan UTxO's
:class:`LoanDatum` + the loan's out-ref + the lender-bond policy, so it reproduces the
on-chain datum byte-exact (verified in test_datum_synth).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pycardano import Datum
from pycardano import PlutusData

from charli3_dendrite.lending.fluidtokens.datums import CollateralAsset
from charli3_dendrite.lending.fluidtokens.datums import CommonData
from charli3_dendrite.lending.fluidtokens.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum

# Origin-id tag a pool-origin loan datum carries: the loan's ``origin_id`` is this tag
# followed by the originating pool's NFT asset name (`b"POOL" + pool_id`).
ORIGIN_POOL_TAG = b"POOL"

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


# The receipt's tag bytestring identifying a recast.
RECAST_TAG = b"recast"


@dataclass
class RecastReceiptDatum(PlutusData):
    """The recast-receipt datum on the lender output == Constr0([...]).

    Records the recast against the loan: the loan out-ref, the ``recast`` tag, the loan
    id, and the lender-bond it credits.
    """

    CONSTR_ID = 0
    loan_out_ref: TxOutRef
    tag: bytes
    loan_id: bytes
    lender_bond: LenderBondRef


def synth_recast_receipt(
    *,
    loan_out_ref: tuple[str, int],
    loan_id: bytes,
    lender_bond_policy: str,
) -> RecastReceiptDatum:
    """Build the recast-receipt datum for the lender output of a recast.

    Pins the receipt to the loan being recast (its out-ref + id) and the lender-bond it
    credits; reproduces the on-chain datum byte-exact (verified in test_datum_synth).
    """
    return RecastReceiptDatum(
        loan_out_ref=TxOutRef(
            tx_id=bytes.fromhex(loan_out_ref[0]),
            index=loan_out_ref[1],
        ),
        tag=RECAST_TAG,
        loan_id=loan_id,
        lender_bond=LenderBondRef(
            lender_bond_policy=bytes.fromhex(lender_bond_policy),
            loan_id=loan_id,
        ),
    )


def synth_loan_datum(
    *,
    pool_datum: PoolDatum,
    pool_id: bytes,
    principal_amount: int,
    lend_date: int,
    chosen_collateral_index: int,
) -> LoanDatum:
    """Build the continuing loan's :class:`LoanDatum` for a pool-origin borrow.

    Reproduces the on-chain loan output datum byte-exact (verified in test_borrow): a
    fresh loan starts with ``done_recasts`` / ``repaid_installments`` at 0, carries the
    borrowed ``principal_amount`` and a ``lend_date`` equal to the validity upper bound
    (POSIX ms), and inherits every loan term from the pool's ``common_data``. The
    ``origin_id`` is ``b"POOL"`` + the pool NFT name, and the collateral is the chosen
    pool collateral option carried through verbatim.
    """
    common = pool_datum.common_data
    options = list(pool_datum.collateral_options)
    chosen = options[chosen_collateral_index]
    if not isinstance(chosen, CollateralAsset):
        chosen = CollateralAsset.from_primitive(chosen)
    return LoanDatum(
        done_recasts=0,
        principal_amount=principal_amount,
        lend_date=lend_date,
        repaid_installments=0,
        interest_rate=common.interest_rate,
        total_installments=common.total_installments,
        principal_asset=common.principal_asset,
        principal_oracle_asset=common.principal_oracle_asset,
        installment_period=common.installment_period,
        initial_grace_period=common.initial_grace_period,
        liquidation_mode=common.liquidation_mode,
        repayment_mode=common.repayment_mode,
        repayment_time_window=common.repayment_time_window,
        penalty_fee_for_late_repayment=common.penalty_fee_for_late_repayment,
        repayment_receipts=common.repayment_receipts,
        origin_id=ORIGIN_POOL_TAG + pool_id,
        collateral=chosen,
    )


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


@dataclass
class PoolTerms:
    """Lender-chosen terms for a new pool, mapped 1:1 onto :class:`PoolDatum`.

    Holds the raw (already PlutusData-typed) sub-objects so the live create path can
    pass through the unions (``lender_auth``, ``repayment_mode``, ``liquidation_mode``,
    etc.) without re-encoding them lossily. ``from_pool_datum`` recovers a
    :class:`PoolTerms` from a decoded on-chain :class:`PoolDatum` for round-trip
    verification. This is the groundwork for the live pool-create path: it is consumed
    by :func:`synth_pool_datum` to build the inline :class:`PoolDatum`, whereas the
    capture-replay path carries the datum verbatim instead.
    """

    permissioned_condition_script_hash: bytes
    extra_data: Datum
    common_data: CommonData
    lender_auth: Datum
    lender_bond_address: Datum
    lender_bond_inline_datum_hash: bytes
    collateral_options: list[CollateralAsset]
    min_collateral: list[int]
    min_collateral_divider: list[int]
    dynamic_collateral_price: Datum

    @classmethod
    def from_pool_datum(cls, datum: PoolDatum) -> PoolTerms:
        """Recover lender terms from a decoded on-chain :class:`PoolDatum`."""

        def _as_list(field: Iterable) -> list:
            return list(field)  # works for plain list and pycardano IndefiniteList

        return cls(
            permissioned_condition_script_hash=datum.permissioned_condition_script_hash,
            extra_data=datum.extra_data,
            common_data=datum.common_data,
            lender_auth=datum.lender_auth,
            lender_bond_address=datum.lender_bond_address,
            lender_bond_inline_datum_hash=datum.lender_bond_inline_datum_hash,
            collateral_options=_as_list(datum.collateral_options),
            min_collateral=_as_list(datum.min_collateral),
            min_collateral_divider=_as_list(datum.min_collateral_divider),
            dynamic_collateral_price=datum.dynamic_collateral_price,
        )


def synth_pool_datum(terms: PoolTerms) -> PoolDatum:
    """Build a :class:`PoolDatum` from lender :class:`PoolTerms`.

    ``PoolDatum.__post_init__`` pins the conditional list encodings (empty -> definite,
    non-empty -> ``IndefiniteList``), so passing plain lists through is sufficient.
    """
    return PoolDatum(
        permissioned_condition_script_hash=terms.permissioned_condition_script_hash,
        extra_data=terms.extra_data,
        common_data=terms.common_data,
        lender_auth=terms.lender_auth,
        lender_bond_address=terms.lender_bond_address,
        lender_bond_inline_datum_hash=terms.lender_bond_inline_datum_hash,
        collateral_options=terms.collateral_options,
        min_collateral=terms.min_collateral,
        min_collateral_divider=terms.min_collateral_divider,
        dynamic_collateral_price=terms.dynamic_collateral_price,
    )
