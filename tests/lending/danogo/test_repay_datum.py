"""Synthesize the DecreaseLoanAmount pool + loan datums, pinned byte-exact.

Fully offline: the before/after pool datums, the loan in/out datums (CBOR), and the
realized intermediates are embedded in the repay fixtures, so no backend/network is
required. Two captured variants are covered: a full repay (no loan output) and a
partial repay (a reduced loan output).
"""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.math import current_loan_amount
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_decrease_loan,
)
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    update_loan_datum_repay,
)

FIX_DIR = Path(__file__).parent / "fixtures"

FULL = "decrease_loan_tx.json"
PARTIAL = "decrease_loan_partial_tx.json"


def _load(fixture: str) -> dict:
    return json.loads((FIX_DIR / fixture).read_text())


def _synth_pool(fix: dict) -> tuple[PoolDatum, int]:
    """Run the pool-datum synth from a fixture's spent datum + realized params.

    Both captured markets declare alt-supply tokens but the pool held a single
    supply token in each capture, so ``alt_tokens_interest`` is 0 and the alt-rate
    list is carried through unchanged (``new_alt_supply_tokens_rate=None``).
    """
    realized = fix["realized"]
    prev = PoolDatum.from_cbor(bytes.fromhex(fix["pool_in_datum"]))
    return synth_pool_datum_decrease_loan(
        prev,
        pool_change_amount=realized["pool_change_amount"],
        txn_time=realized["txn_time"],
        power_base=realized["power_base"],
        base_rate=realized["base_rate"],
        loan_fee_rate=realized["loan_fee_rate"],
        alt_tokens_interest=realized["alt_tokens_interest"],
        new_alt_supply_tokens_rate=None,
    )


def _check_pool(fixture: str) -> None:
    fix = _load(fixture)
    realized = fix["realized"]
    got, fee_output_amount = _synth_pool(fix)
    # The advanced pool datum reproduces the captured output byte-exact, and the
    # swept fee-output amount equals the real on-chain payout -- both from one call.
    assert got.to_cbor().hex() == fix["pool_out_datum"]
    assert fee_output_amount == realized["fee_output_amount"]
    assert fee_output_amount == realized["prev_undistributed_fee"] + (
        realized["new_loan_interest_fee"]
    )


def test_synth_pool_datum_full_repay_matches_captured():
    _check_pool(FULL)


def test_synth_pool_datum_partial_repay_matches_captured():
    _check_pool(PARTIAL)


def test_update_loan_datum_partial_repay_matches_captured():
    """Byte-exact reproduction of the reduced loan datum on a partial repay."""
    fix = _load(PARTIAL)
    realized = fix["realized"]
    loan_in = LoanDatum.from_cbor(bytes.fromhex(fix["loan_in_datum"]))
    pool_out, _ = _synth_pool(fix)

    loan_out = update_loan_datum_repay(
        loan_in,
        current_loan_amount=realized["current_loan_amount"],
        pool_change_amount=realized["pool_change_amount"],
        new_interest_index=pool_out.interest_index,
    )
    assert loan_out.to_cbor().hex() == fix["loan_out_datum"]
    # The owner NFT and borrowed token are carried through unchanged.
    assert loan_out.owner_nft.to_cbor().hex() == loan_in.owner_nft.to_cbor().hex()
    assert loan_out.token == loan_in.token


def test_full_repay_produces_no_loan_output():
    """The full-repay fixture carries no loan output to reproduce."""
    assert _load(FULL)["loan_out_datum"] is None


def test_realized_intermediates_match_formulas():
    """Sanity-check the fixture's realized intermediates against the formulas.

    `current_loan_amount` accrues the loan's debt to repay time; the partial repay's
    new loan amount is that debt minus the repaid amount; and the swept fee pot is
    the prior fee plus the protocol's cut of the newly accrued interest.
    """
    for fixture in (FULL, PARTIAL):
        fix = _load(fixture)
        realized = fix["realized"]
        loan_in = LoanDatum.from_cbor(bytes.fromhex(fix["loan_in_datum"]))
        pool_out = PoolDatum.from_cbor(bytes.fromhex(fix["pool_out_datum"]))

        assert (
            current_loan_amount(
                loan_amount=loan_in.loan_amount,
                current_index=pool_out.interest_index,
                initial_index=loan_in.initial_interest_index,
            )
            == realized["current_loan_amount"]
        )
        assert (
            realized["fee_output_amount"]
            == realized["prev_undistributed_fee"] + realized["new_loan_interest_fee"]
        )

    partial = _load(PARTIAL)["realized"]
    assert partial["current_loan_amount"] - partial["pool_change_amount"] == (
        LoanDatum.from_cbor(bytes.fromhex(_load(PARTIAL)["loan_out_datum"])).loan_amount
    )
