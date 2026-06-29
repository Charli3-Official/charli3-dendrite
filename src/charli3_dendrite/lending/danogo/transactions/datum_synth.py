"""Synthesize Danogo loan/pool datums from a requested action, not a fixture copy.

Two flows are covered, each advancing the spent pool datum's interest accrual
identically before applying its own effect:

- **create-loan** — build the new loan UTxO datum from the requested borrow (owner
  NFT, token, amount, index) and advance the spent pool datum to book the borrow.
- **increase-loan (IncreaseLoanAmount)** — advance the spent pool datum like
  create-loan (fee accumulates, not swept), book the additional borrow, and raise the
  existing loan datum's outstanding amount (resetting its interest index).
- **deposit/withdraw (TopupWithdraw)** — advance the spent pool datum for the
  supply-token change and mint/burn dTokens pro-rata to the existing ratio.
- **repay (DecreaseLoanAmount)** — advance the spent pool datum, sweep the
  accumulated fee pot to the market fee output, and (partial repay) reduce the
  loan datum's outstanding amount.

All synthesized datums (and the dToken mint/burn amount) are pinned byte/value-exact
against captured CBOR in the tests.
"""

from __future__ import annotations

from typing import NamedTuple

from pycardano import IndefiniteList

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import OwnerNft
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import PRational
from charli3_dendrite.lending.danogo.math import current_borrow_apy
from charli3_dendrite.lending.danogo.math import current_interest_index
from charli3_dendrite.lending.math import bps_mul_ceil
from charli3_dendrite.lending.math import floor_div


def synth_loan_datum(
    *,
    owner_policy: bytes,
    owner_name: bytes,
    token_policy: bytes,
    token_name: bytes,
    loan_amount: int,
    initial_interest_index: int,
) -> LoanDatum:
    """Build the loan UTxO datum for a new borrow.

    The owner NFT (`[policy, name]`) and the borrowed token (`[policy, name]`) are
    both encoded as indefinite-length lists, matching the on-chain layout.
    """
    return LoanDatum(
        owner_nft=OwnerNft(asset=IndefiniteList([owner_policy, owner_name])),
        token=IndefiniteList([token_policy, token_name]),
        loan_amount=loan_amount,
        initial_interest_index=initial_interest_index,
    )


class AccruedInterest(NamedTuple):
    """Result of advancing a pool's interest accrual to a new `txn_time`.

    - ``interest_index`` — the new interest index.
    - ``accumulated`` — interest accrued on the outstanding borrow since the last
      update.
    - ``fee`` — the protocol's cut of that accrued interest.
    """

    interest_index: int
    accumulated: int
    fee: int


def _accrue_interest(
    prev: PoolDatum,
    *,
    txn_time: int,
    loan_fee_rate: int,
) -> AccruedInterest:
    """Advance the interest index and split the accrued interest.

    Returns an `AccruedInterest` where:

    - ``interest_index`` accrues from ``prev.interest_time`` to ``txn_time`` at
      ``prev.borrow_apy`` (`current_interest_index`).
    - ``accumulated = floor(prev.total_borrow * (interest_index -
      prev.interest_index) / prev.interest_index)`` is the interest accrued on the
      outstanding borrow since the last update.
    - ``fee = ceil(accumulated * loan_fee_rate / BASIS)`` is the protocol's cut of
      that accrued interest.

    Shared by the create-loan and topup/withdraw pool-datum synthesizers, which
    accrue identically before applying their respective borrow / supply change.
    """
    new_index = current_interest_index(
        prev.interest_index,
        borrow_apy=prev.borrow_apy,
        interest_time=prev.interest_time,
        txn_time=txn_time,
    )
    accumulated_interest = floor_div(
        prev.total_borrow * (new_index - prev.interest_index),
        prev.interest_index,
    )
    loan_interest_fee = bps_mul_ceil(accumulated_interest, loan_fee_rate)
    return AccruedInterest(new_index, accumulated_interest, loan_interest_fee)


def synth_pool_datum_create_loan(
    prev: PoolDatum,
    *,
    loan_amount: int,
    txn_time: int,
    power_base: int,
    base_rate: int,
    loan_fee_rate: int,
    loan_origination_fee_rate: int = 0,
    loan_origination_fee_min_amount: int = 0,
    alt_tokens_interest: int = 0,
    new_alt_supply_tokens_rate: list[PRational] | None = None,
) -> PoolDatum:
    """Compute the pool UTxO datum produced by a create-loan transaction.

    Starting from the spent pool datum `prev`, this advances the interest index to
    `txn_time`, accrues the interest that built up over that interval, books the
    new borrow, and splits the accrued interest between suppliers and the protocol
    fee pot, mirroring the on-chain create-loan rules.

    Intermediate quantities (all exact integer math, `BASIS` = basis points):

    - ``new_index`` accrues from ``prev.interest_time`` to ``txn_time`` at
      ``prev.borrow_apy`` (`current_interest_index`).
    - ``new_accumulated_interest = floor(prev.total_borrow * (new_index -
      prev.interest_index) / prev.interest_index)`` is the interest accrued on the
      outstanding borrow since the last update.
    - ``new_loan_interest_fee = ceil(new_accumulated_interest * loan_fee_rate /
      BASIS)`` is the protocol's cut of that accrued interest.
    - ``loan_origination_fee = max(ceil(loan_amount * loan_origination_fee_rate /
      BASIS), loan_origination_fee_min_amount)`` is the one-off fee for opening the
      loan (0 when the rate and min are 0).

    ``alt_tokens_interest`` is the interest contributed by alternative supply
    tokens -- the change in the supply-token value of the pool's alt holdings when
    each alt token is re-priced from the oracle, ``sum(floor(amount * (new_rate -
    old_rate)))``. It is 0 for pools with no alt tokens. Computing it requires the
    pool's alt holdings and the live oracle prices (out of scope here), so it is
    injected, together with the re-priced ``new_alt_supply_tokens_rate``; callers
    with alt tokens must supply both.

    `circulating_dtoken` is carried through unchanged.
    ``alt_supply_tokens_rate`` is replaced with ``new_alt_supply_tokens_rate`` when
    given (the oracle re-prices it on every create-loan), else carried through.
    """
    accrued = _accrue_interest(
        prev,
        txn_time=txn_time,
        loan_fee_rate=loan_fee_rate,
    )
    # TODO(market): the market parser does not yet surface an origination-fee
    # minimum, so `loan_origination_fee_min_amount` defaults to 0 here; once that
    # field is wired in `market.py`, callers can pass the real on-chain minimum.
    loan_origination_fee = max(
        bps_mul_ceil(loan_amount, loan_origination_fee_rate),
        loan_origination_fee_min_amount,
    )

    new_total_supply = (
        prev.total_supply + accrued.accumulated - accrued.fee + alt_tokens_interest
    )
    new_total_borrow = prev.total_borrow + accrued.accumulated + loan_amount
    borrow_apy = current_borrow_apy(
        power_base=power_base,
        base_rate=base_rate,
        total_borrow=new_total_borrow,
        total_supply=new_total_supply,
    )

    return PoolDatum(
        total_supply=new_total_supply,
        circulating_dtoken=prev.circulating_dtoken,
        total_borrow=new_total_borrow,
        borrow_apy=borrow_apy,
        undistributed_fee=prev.undistributed_fee + accrued.fee + loan_origination_fee,
        interest_index=accrued.interest_index,
        interest_time=txn_time,
        # The oracle re-prices the alt holdings on every create-loan, so swap in the
        # re-priced rates when given; otherwise carry the prior list through. The mint
        # validator decodes and compares the datum by value, so the (definite) list
        # encoding pycardano emits is accepted.
        alt_supply_tokens_rate=(
            prev.alt_supply_tokens_rate
            if new_alt_supply_tokens_rate is None
            else new_alt_supply_tokens_rate
        ),
    )


def synth_pool_datum_increase_loan(
    prev: PoolDatum,
    *,
    borrow_delta: int,
    txn_time: int,
    power_base: int,
    base_rate: int,
    loan_fee_rate: int,
    loan_origination_fee: int = 0,
    alt_tokens_interest: int = 0,
    new_alt_supply_tokens_rate: list[PRational] | None = None,
) -> PoolDatum:
    """Compute the pool UTxO datum produced by an increase-loan transaction.

    Borrowing more against an existing loan advances the pool exactly like create-loan
    (NOT like repay): starting from the spent pool datum `prev`, this advances the
    interest index to `txn_time`, accrues the interest that built up over that interval
    and splits it between suppliers and the protocol fee pot, then books the additional
    borrow. The accrued fee pot ACCUMULATES into ``undistributed_fee`` -- it is NOT
    swept to a fee output as on repay.

    `borrow_delta` is the rise in the loan's interest-accrued principal
    (``loan_out.loan_amount - current_loan_amount``); it includes any origination fee
    folded into the loan principal. It is added to `total_borrow` on top of the accrued
    interest, mirroring how create-loan books a fresh ``loan_amount``. The pool's supply
    only moves by the accrued interest minus its fee (plus any alt-token interest); the
    borrowed amount leaves the pool's raw supply-token holdings, not its accounted
    ``total_supply``. `circulating_dtoken` is carried through unchanged (no mint/burn).

    `loan_origination_fee` is the one-off fee for borrowing more (0 on markets whose
    origination rate is 0); it is added to `undistributed_fee` alongside the interest
    fee. On current mainnet markets the rate is 0, so it defaults to 0 and no fee output
    is produced.

    ``alt_tokens_interest`` is the interest contributed by re-pricing the pool's
    alternative supply tokens from the oracle, ``sum(floor(amount * (new_rate -
    old_rate)))`` -- the same term create-loan books (see
    `synth_pool_datum_create_loan`). It is 0 for single-supply-token pools (and for
    alt-supply pools that hold none of the alt token). Computing it needs the pool's alt
    holdings + live oracle prices (out of scope here), so it is injected together with
    the re-priced ``new_alt_supply_tokens_rate``; alt-supply callers must supply both.

    ``alt_supply_tokens_rate`` is replaced with ``new_alt_supply_tokens_rate`` when
    given (the oracle re-prices it), else carried through unchanged.
    """
    accrued = _accrue_interest(
        prev,
        txn_time=txn_time,
        loan_fee_rate=loan_fee_rate,
    )

    new_total_supply = (
        prev.total_supply + accrued.accumulated - accrued.fee + alt_tokens_interest
    )
    new_total_borrow = prev.total_borrow + accrued.accumulated + borrow_delta
    borrow_apy = current_borrow_apy(
        power_base=power_base,
        base_rate=base_rate,
        total_borrow=new_total_borrow,
        total_supply=new_total_supply,
    )

    return PoolDatum(
        total_supply=new_total_supply,
        circulating_dtoken=prev.circulating_dtoken,
        total_borrow=new_total_borrow,
        borrow_apy=borrow_apy,
        undistributed_fee=prev.undistributed_fee + accrued.fee + loan_origination_fee,
        interest_index=accrued.interest_index,
        interest_time=txn_time,
        alt_supply_tokens_rate=(
            prev.alt_supply_tokens_rate
            if new_alt_supply_tokens_rate is None
            else new_alt_supply_tokens_rate
        ),
    )


def update_loan_datum_increase(
    loan_in: LoanDatum,
    *,
    current_loan_amount: int,
    pool_changed_amount: int,
    new_interest_index: int,
    loan_origination_fee: int = 0,
) -> LoanDatum:
    """Raise a loan datum's outstanding amount for an increase-loan.

    The outstanding amount rises to ``current_loan_amount - pool_changed_amount +
    loan_origination_fee`` (where ``current_loan_amount`` is the interest-accrued debt
    at increase time and ``pool_changed_amount < 0`` is the supply the pool pays out,
    so the amount grows), and the loan's `initial_interest_index` is reset to the
    pool's advanced interest index, so subsequent accrual restarts from this point. The
    owner NFT and the borrowed token are carried through unchanged.
    """
    return LoanDatum(
        owner_nft=loan_in.owner_nft,
        token=loan_in.token,
        loan_amount=current_loan_amount - pool_changed_amount + loan_origination_fee,
        initial_interest_index=new_interest_index,
    )


def synth_pool_datum_decrease_loan(
    prev: PoolDatum,
    *,
    pool_change_amount: int,
    txn_time: int,
    power_base: int,
    base_rate: int,
    loan_fee_rate: int,
    alt_tokens_interest: int = 0,
    new_alt_supply_tokens_rate: list[PRational] | None = None,
) -> tuple[PoolDatum, int]:
    """Compute the pool datum + fee-output amount produced by a repay transaction.

    Starting from the spent pool datum `prev`, this advances the interest index to
    `txn_time`, accrues the interest that built up over that interval (splitting it
    between suppliers and the protocol fee pot), then books the repayment, mirroring
    the on-chain DecreaseLoanAmount rules. Unlike a deposit, the repayment does NOT
    raise the pool's supply -- it pays down the outstanding borrow -- so the supply
    only moves by the accrued interest minus its fee (plus any alt-token interest).

    `pool_change_amount` is the supply-token amount the borrower repays; it reduces
    `total_borrow` (after accrual) one-for-one. `circulating_dtoken` is carried
    through unchanged (repay neither mints nor burns dTokens).

    The accrued fee pot is SWEPT on every repay: `undistributed_fee` resets to 0 and
    the pot it held -- `prev.undistributed_fee + new_loan_interest_fee` -- is paid out
    to the market fee address by the builder. That amount is returned as the second
    tuple element.

    ``alt_tokens_interest`` is the interest contributed by re-pricing the pool's
    alternative supply tokens from the oracle, ``sum(floor(amount * (new_rate -
    old_rate)))`` -- the same term create-loan and topup/withdraw book. It is 0 for
    single-supply-token pools. Computing it needs the pool's alt holdings + live
    oracle prices (out of scope here), so it is injected together with the re-priced
    ``new_alt_supply_tokens_rate``; alt-supply callers must supply both.

    ``alt_supply_tokens_rate`` is replaced with ``new_alt_supply_tokens_rate`` when
    given (the oracle re-prices it), else carried through unchanged.
    """
    accrued = _accrue_interest(
        prev,
        txn_time=txn_time,
        loan_fee_rate=loan_fee_rate,
    )

    total_supply_before = (
        prev.total_supply + accrued.accumulated - accrued.fee + alt_tokens_interest
    )
    # The repayment pays down borrow; it does not add to the pool's supply.
    new_total_supply = total_supply_before
    new_total_borrow = prev.total_borrow + accrued.accumulated - pool_change_amount
    borrow_apy = current_borrow_apy(
        power_base=power_base,
        base_rate=base_rate,
        total_borrow=new_total_borrow,
        total_supply=new_total_supply,
    )

    fee_output_amount = prev.undistributed_fee + accrued.fee

    new_pool_datum = PoolDatum(
        total_supply=new_total_supply,
        circulating_dtoken=prev.circulating_dtoken,
        total_borrow=new_total_borrow,
        borrow_apy=borrow_apy,
        # The accrued fee pot is swept to the market fee output on repay.
        undistributed_fee=0,
        interest_index=accrued.interest_index,
        interest_time=txn_time,
        alt_supply_tokens_rate=(
            prev.alt_supply_tokens_rate
            if new_alt_supply_tokens_rate is None
            else new_alt_supply_tokens_rate
        ),
    )
    return new_pool_datum, fee_output_amount


def update_loan_datum_repay(
    loan_in: LoanDatum,
    *,
    current_loan_amount: int,
    pool_change_amount: int,
    new_interest_index: int,
) -> LoanDatum:
    """Reduce a loan datum for a PARTIAL repay (full repay produces no loan output).

    The outstanding amount drops to ``current_loan_amount - pool_change_amount``
    (where ``current_loan_amount`` is the interest-accrued debt at repay time) and
    the loan's `initial_interest_index` is reset to the pool's advanced interest
    index, so subsequent accrual restarts from this point. The owner NFT and the
    borrowed token are carried through unchanged.

    On full repay the builder omits the loan output entirely and does not call this.
    """
    return LoanDatum(
        owner_nft=loan_in.owner_nft,
        token=loan_in.token,
        loan_amount=current_loan_amount - pool_change_amount,
        initial_interest_index=new_interest_index,
    )


def mint_burn_dtoken(
    *,
    pool_changed_amount: int,
    withdraw_fee: int,
    total_supply_before: int,
    circulating_dtoken: int,
) -> int:
    """DTokens minted (deposit, +) or burned (withdraw, -) for a supply change.

    On bootstrap -- when the pool holds no supply or no dTokens circulate -- the
    minted amount equals the deposited supply 1:1. Otherwise the pool mints/burns
    dTokens pro-rata to the existing dToken/supply ratio:
    ``floor((pool_changed_amount - withdraw_fee) * circulating_dtoken /
    total_supply_before)``. ``total_supply_before`` is the pool's supply AFTER the
    interest accrual for this tx but BEFORE the deposit/withdrawal flow.
    """
    if total_supply_before == 0 or circulating_dtoken == 0:
        # Bootstrap is always a deposit, so `withdraw_fee == 0`; no need to subtract.
        return pool_changed_amount
    return floor_div(
        (pool_changed_amount - withdraw_fee) * circulating_dtoken,
        total_supply_before,
    )


def synth_pool_datum_topup_withdraw(
    prev: PoolDatum,
    *,
    pool_changed_amount: int,
    withdraw_fee: int,
    txn_time: int,
    power_base: int,
    base_rate: int,
    loan_fee_rate: int,
    alt_tokens_interest: int = 0,
    new_alt_supply_tokens_rate: list[PRational] | None = None,
) -> tuple[PoolDatum, int]:
    """Compute the pool datum + dToken mint/burn produced by a deposit/withdrawal.

    Starting from the spent pool datum `prev`, this advances the interest index to
    `txn_time`, accrues the interest that built up over that interval (splitting it
    between suppliers and the protocol fee pot), then applies the supply change and
    mints/burns dTokens pro-rata, mirroring the on-chain TopupWithdraw rules.

    `pool_changed_amount` is the realized supply-token delta (``+`` for a deposit,
    ``-`` for a withdrawal); `withdraw_fee` is the protocol's flat withdrawal fee
    (0 for deposits). The dToken delta is computed against the supply AFTER accrual
    but BEFORE the flow (`mint_burn_dtoken`).

    ``alt_tokens_interest`` is the interest contributed by re-pricing the pool's
    alternative supply tokens from the oracle, ``sum(floor(amount * (new_rate -
    old_rate)))`` -- the same term create-loan books (see
    `synth_pool_datum_create_loan`). It is 0 for single-supply-token pools. It is
    folded into the supply BEFORE the flow, so it raises both the new total supply
    and the dToken mint ratio. Computing it needs the pool's alt holdings + live
    oracle prices (out of scope here), so it is injected together with the re-priced
    ``new_alt_supply_tokens_rate``; alt-supply callers must supply both.

    ``alt_supply_tokens_rate`` is replaced with ``new_alt_supply_tokens_rate`` when
    given (the oracle re-prices it), else carried through unchanged.

    Returns the advanced pool datum and the signed dToken mint/burn amount the datum
    booked into ``circulating_dtoken`` (``+`` mint on deposit, ``-`` burn on
    withdrawal). Both come from the SAME accrual, so the amount the caller mints/burns
    always matches the datum -- no risk of the two formulas drifting apart.
    """
    accrued = _accrue_interest(
        prev,
        txn_time=txn_time,
        loan_fee_rate=loan_fee_rate,
    )

    total_supply_before = (
        prev.total_supply + accrued.accumulated - accrued.fee + alt_tokens_interest
    )
    new_total_supply = total_supply_before + pool_changed_amount - withdraw_fee
    minted_dtoken = mint_burn_dtoken(
        pool_changed_amount=pool_changed_amount,
        withdraw_fee=withdraw_fee,
        total_supply_before=total_supply_before,
        circulating_dtoken=prev.circulating_dtoken,
    )

    new_total_borrow = prev.total_borrow + accrued.accumulated
    borrow_apy = current_borrow_apy(
        power_base=power_base,
        base_rate=base_rate,
        total_borrow=new_total_borrow,
        total_supply=new_total_supply,
    )

    new_pool_datum = PoolDatum(
        total_supply=new_total_supply,
        circulating_dtoken=prev.circulating_dtoken + minted_dtoken,
        total_borrow=new_total_borrow,
        borrow_apy=borrow_apy,
        undistributed_fee=prev.undistributed_fee + accrued.fee + withdraw_fee,
        interest_index=accrued.interest_index,
        interest_time=txn_time,
        alt_supply_tokens_rate=(
            prev.alt_supply_tokens_rate
            if new_alt_supply_tokens_rate is None
            else new_alt_supply_tokens_rate
        ),
    )
    return new_pool_datum, minted_dtoken
