"""Synthesize the IncreaseLoanAmount pool + loan datums, pinned byte-exact.

Fully offline: the before/after pool datums, the loan in/out datums (CBOR), and the
realized intermediates are embedded in the increase-loan fixture, so no
backend/network is required. Increase-loan advances the pool like create-loan (the
accrued fee accumulates, it is NOT swept) and raises the existing loan's amount.
"""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.math import current_loan_amount
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_increase_loan,
)
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    update_loan_datum_increase,
)

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "increase_loan_tx.json").read_text()
)


def _synth_pool() -> PoolDatum:
    """Run the pool-datum synth from the fixture's spent datum + realized params.

    The captured market declares an alt-supply token but the pool held none in this
    capture, so ``alt_tokens_interest`` is 0 and the alt-rate list is carried through
    unchanged (``new_alt_supply_tokens_rate=None``).
    """
    realized = FIX["realized"]
    prev = PoolDatum.from_cbor(bytes.fromhex(FIX["pool_in_datum"]))
    return synth_pool_datum_increase_loan(
        prev,
        borrow_delta=realized["borrow_delta"],
        txn_time=realized["txn_time"],
        power_base=realized["power_base"],
        base_rate=realized["base_rate"],
        loan_fee_rate=realized["loan_fee_rate"],
        loan_origination_fee=realized["loan_origination_fee"],
        alt_tokens_interest=realized["alt_tokens_interest"],
        new_alt_supply_tokens_rate=None,
    )


def test_synth_pool_datum_increase_loan_matches_captured():
    """Pin the full increase-loan pool datum byte-exact against the capture."""
    got = _synth_pool()
    assert got.to_cbor().hex() == FIX["pool_out_datum"]


def test_synth_pool_datum_increase_loan_books_borrow_and_accumulates_fee():
    """The pool gains the accrued interest + borrow delta and accumulates the fee."""
    realized = FIX["realized"]
    prev = PoolDatum.from_cbor(bytes.fromhex(FIX["pool_in_datum"]))
    got = _synth_pool()

    assert got.total_borrow == (
        prev.total_borrow
        + realized["new_accumulated_interest"]
        + realized["borrow_delta"]
    )
    # The fee pot ACCUMULATES (unlike repay, which sweeps it to 0).
    assert got.undistributed_fee == (
        prev.undistributed_fee
        + realized["new_loan_interest_fee"]
        + realized["loan_origination_fee"]
    )
    assert got.undistributed_fee > prev.undistributed_fee
    # No mint/burn: the circulating dToken supply is untouched.
    assert got.circulating_dtoken == prev.circulating_dtoken
    assert got.interest_index == realized["current_interest_index"]
    assert got.interest_time == realized["txn_time"]


def test_update_loan_datum_increase_matches_captured():
    """Byte-exact reproduction of the raised loan datum."""
    realized = FIX["realized"]
    loan_in = LoanDatum.from_cbor(bytes.fromhex(FIX["loan_in_datum"]))
    pool_out = _synth_pool()

    loan_out = update_loan_datum_increase(
        loan_in,
        current_loan_amount=realized["current_loan_amount"],
        pool_changed_amount=realized["pool_changed_amount"],
        new_interest_index=pool_out.interest_index,
        loan_origination_fee=realized["loan_origination_fee"],
    )
    assert loan_out.to_cbor().hex() == FIX["loan_out_datum"]
    # The borrowed amount increases; the interest index resets to the pool's.
    assert loan_out.loan_amount > loan_in.loan_amount
    assert loan_out.loan_amount == FIX["loan_out_amount"]
    assert loan_out.initial_interest_index == pool_out.interest_index
    # The owner NFT and borrowed token are carried through unchanged.
    assert loan_out.owner_nft.to_cbor().hex() == loan_in.owner_nft.to_cbor().hex()
    assert loan_out.token == loan_in.token


def test_realized_intermediates_match_formulas():
    """Sanity-check the fixture's realized intermediates against the formulas."""
    realized = FIX["realized"]
    loan_in = LoanDatum.from_cbor(bytes.fromhex(FIX["loan_in_datum"]))
    loan_out = LoanDatum.from_cbor(bytes.fromhex(FIX["loan_out_datum"]))
    pool_out = PoolDatum.from_cbor(bytes.fromhex(FIX["pool_out_datum"]))

    assert (
        current_loan_amount(
            loan_amount=loan_in.loan_amount,
            current_index=pool_out.interest_index,
            initial_index=loan_in.initial_interest_index,
        )
        == realized["current_loan_amount"]
    )
    # borrow_delta is the rise in the loan's interest-accrued principal.
    assert (
        loan_out.loan_amount - realized["current_loan_amount"]
        == realized["borrow_delta"]
    )
    # The pool pays out supply to the borrower (raw holdings drop).
    assert realized["pool_changed_amount"] < 0
    assert realized["pool_supply_holdings_delta"] == realized["pool_changed_amount"]
    # Current mainnet markets charge no origination fee.
    assert realized["loan_origination_fee_rate"] == 0
    assert realized["loan_origination_fee"] == 0
