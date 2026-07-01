"""Synthesize loan + post-borrow pool datums, pinned against captured CBOR.

Fully offline: every value is derived from the committed create-loan fixture, so
no backend/network access is required.
"""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanContext
from charli3_dendrite.lending.danogo.transactions.datum_synth import synth_loan_datum
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_create_loan,
)
from charli3_dendrite.lending.math import bps_mul_ceil

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)


def test_synth_loan_datum_matches_captured():
    ctx = CreateLoanContext.from_fixture(FIX)
    captured = LoanDatum.from_cbor(bytes.fromhex(ctx.outputs[ctx.loan_out_idx].datum))
    got = synth_loan_datum(
        owner_policy=captured.owner_nft.asset[0],
        owner_name=captured.owner_nft.asset[1],
        token_policy=captured.token[0],
        token_name=captured.token[1],
        loan_amount=captured.loan_amount,
        initial_interest_index=captured.initial_interest_index,
    )
    assert got.to_cbor() == captured.to_cbor()


def _pool_before_after(ctx: CreateLoanContext) -> tuple[PoolDatum, PoolDatum]:
    after = PoolDatum.from_cbor(bytes.fromhex(ctx.outputs[ctx.pool_out_idx].datum))
    pool_in = next(u for u in ctx.inputs if u.out_ref == ctx.pool_in_out_ref)
    before = PoolDatum.from_cbor(bytes.fromhex(pool_in.datum))
    return before, after


def test_synth_pool_datum_create_loan_matches_captured():
    """Pin the full create-loan pool datum byte-exact against the capture.

    Both pool datums are cleanly identifiable (the pool input by its out-ref, the
    pool output by `pool_out_idx`). The captured market DOES carry alt supply
    tokens (`alt_supply_tokens_rate` is non-empty), but their rate is carried
    through unchanged this tx, so no alt interest is realized and
    `alt_tokens_interest` is 0 — letting the interest/fee accrual reconcile
    byte-for-byte with the captured output.
    """
    ctx = CreateLoanContext.from_fixture(FIX)
    before, after = _pool_before_after(ctx)
    market = DanogoMarket.from_market_datum(ctx.ref_inputs[ctx.market_ref_idx].datum)
    loan = LoanDatum.from_cbor(bytes.fromhex(ctx.outputs[ctx.loan_out_idx].datum))

    got = synth_pool_datum_create_loan(
        before,
        loan_amount=loan.loan_amount,
        txn_time=after.interest_time,
        power_base=market.power_base,
        base_rate=market.base_rate,
        loan_fee_rate=market.loan_fee_rate,
        loan_origination_fee_rate=market.loan_origination_fee_rate,
    )

    assert got.to_cbor() == after.to_cbor()


def _create_loan_inputs() -> tuple[PoolDatum, DanogoMarket, int, int]:
    """Captured pool input, market, loan amount, and txn_time for fee tests."""
    ctx = CreateLoanContext.from_fixture(FIX)
    before, after = _pool_before_after(ctx)
    market = DanogoMarket.from_market_datum(ctx.ref_inputs[ctx.market_ref_idx].datum)
    loan = LoanDatum.from_cbor(bytes.fromhex(ctx.outputs[ctx.loan_out_idx].datum))
    return before, market, loan.loan_amount, after.interest_time


def test_origination_fee_percentage_dominates():
    """A non-zero origination rate adds its percentage term on top of the
    interest fee, hitting only `undistributed_fee`."""
    before, market, loan_amount, txn_time = _create_loan_inputs()

    common = dict(
        loan_amount=loan_amount,
        txn_time=txn_time,
        power_base=market.power_base,
        base_rate=market.base_rate,
        loan_fee_rate=market.loan_fee_rate,
    )
    base = synth_pool_datum_create_loan(before, **common)
    rate = 50  # 0.5% in bps
    got = synth_pool_datum_create_loan(
        before,
        loan_origination_fee_rate=rate,
        loan_origination_fee_min_amount=0,
        **common,
    )

    expected_origination = bps_mul_ceil(loan_amount, rate)
    # The interest-fee component (carried in `base`) is non-zero, so the result
    # genuinely includes BOTH the interest fee and the origination percentage.
    assert base.undistributed_fee > before.undistributed_fee
    assert expected_origination > 0
    assert got.undistributed_fee == base.undistributed_fee + expected_origination
    # Origination fee only moves `undistributed_fee`.
    assert got.total_supply == base.total_supply
    assert got.total_borrow == base.total_borrow


def test_origination_fee_min_amount_dominates():
    """When the percentage term is below the configured minimum, the minimum is
    charged instead."""
    before, market, loan_amount, txn_time = _create_loan_inputs()

    common = dict(
        loan_amount=loan_amount,
        txn_time=txn_time,
        power_base=market.power_base,
        base_rate=market.base_rate,
        loan_fee_rate=market.loan_fee_rate,
    )
    base = synth_pool_datum_create_loan(before, **common)
    min_amount = bps_mul_ceil(loan_amount, 1) + 10_000  # strictly above the 1bp term
    got = synth_pool_datum_create_loan(
        before,
        loan_origination_fee_rate=1,
        loan_origination_fee_min_amount=min_amount,
        **common,
    )

    assert bps_mul_ceil(loan_amount, 1) < min_amount
    assert got.undistributed_fee == base.undistributed_fee + min_amount
    assert got.total_supply == base.total_supply
    assert got.total_borrow == base.total_borrow
