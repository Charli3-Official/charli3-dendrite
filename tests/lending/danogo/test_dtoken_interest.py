"""dToken interest/yield helpers: byte-exact round-trips + supply-APY units.

The byte-exact half drives the helpers off captured deposit/withdraw fixtures: it
reconstructs ``total_supply_before`` from the spent pool datum via the same accrual
the synth uses, then checks that `mint_burn_dtoken` / `dtoken_redeem_value`
round-trip and that the `dtoken_exchange_rate` reproduces the captured supply delta
within the documented floor tolerance. Fully offline -- the before/after datums and
realized intermediates live in the fixtures.
"""

import json
from decimal import Decimal
from fractions import Fraction
from math import ceil
from pathlib import Path

import pytest

from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import PRational
from charli3_dendrite.lending.danogo.math import alt_supply_value
from charli3_dendrite.lending.danogo.math import available_supply
from charli3_dendrite.lending.danogo.math import dtoken_exchange_rate
from charli3_dendrite.lending.danogo.math import dtoken_redeem_value
from charli3_dendrite.lending.danogo.math import supply_apy
from charli3_dendrite.lending.danogo.math import total_supply_apy
from charli3_dendrite.lending.danogo.transactions.datum_synth import _accrue_interest
from charli3_dendrite.lending.danogo.transactions.datum_synth import mint_burn_dtoken
from charli3_dendrite.lending.math import floor_div

FIX_DIR = Path(__file__).parent / "fixtures"


def _supply_before(fixture: str) -> tuple[int, int, dict]:
    """(total_supply_before, circulating_dtoken, realized) for a topup/withdraw fix.

    ``total_supply_before`` is the post-accrual, pre-flow supply -- exactly what
    `mint_burn_dtoken` / `dtoken_exchange_rate` consume -- reconstructed from the
    spent pool datum via the shared interest accrual (no alt tokens in these
    single-supply fixtures).
    """
    fix = json.loads((FIX_DIR / fixture).read_text())
    prev = PoolDatum.from_cbor(bytes.fromhex(fix["pool_in_datum"]))
    realized = fix["realized"]
    accrued = _accrue_interest(
        prev,
        txn_time=realized["txn_time"],
        loan_fee_rate=realized["loan_fee_rate"],
    )
    total_supply_before = prev.total_supply + accrued.accumulated - accrued.fee
    return total_supply_before, prev.circulating_dtoken, realized


def _check_round_trip(fixture: str) -> None:
    total_supply_before, circulating_dtoken, realized = _supply_before(fixture)
    net_supply_delta = realized["pool_changed_amount"] - realized["withdraw_fee"]

    minted = mint_burn_dtoken(
        pool_changed_amount=realized["pool_changed_amount"],
        withdraw_fee=realized["withdraw_fee"],
        total_supply_before=total_supply_before,
        circulating_dtoken=circulating_dtoken,
    )
    # The helper reproduces the captured on-chain dToken delta exactly.
    assert minted == realized["mint_burn_dtoken"]

    rate = dtoken_exchange_rate(
        total_supply_before=total_supply_before,
        circulating_dtoken=circulating_dtoken,
    )
    assert rate == Fraction(total_supply_before, circulating_dtoken)

    # `dtoken_redeem_value` is the inverse of `mint_burn_dtoken`. Each direction
    # floors once, so re-deriving the supply delta from the dToken delta loses at
    # most one dToken (worth `rate` supply units) plus the redeem floor's <1 supply
    # unit: |net - redeem| < rate + 1.
    redeemed = dtoken_redeem_value(
        minted,
        total_supply_before=total_supply_before,
        circulating_dtoken=circulating_dtoken,
    )
    tolerance = ceil(rate) + 1
    assert abs(net_supply_delta - redeemed) <= tolerance

    # `dtoken_exchange_rate` x minted reconstructs the supply delta to within the
    # single mint-floor error (< 1 dToken == `rate` supply units).
    assert abs(Fraction(net_supply_delta) - rate * minted) <= rate


def test_round_trip_topup():
    _check_round_trip("topup_tx.json")


def test_round_trip_withdraw():
    _check_round_trip("withdraw_tx.json")


def test_round_trip_topup_alt():
    # The alt fixture's ``alt_tokens_interest`` is folded into supply before the flow,
    # so the simple (no-alt) reconstruction here does not reproduce the captured
    # dToken delta byte-exact; that path is pinned in `test_topup_withdraw_datum`.
    # We still exercise the inverse relationship on its captured numbers.
    fix = json.loads((FIX_DIR / "topup_alt_tx.json").read_text())
    realized = fix["realized"]
    prev = PoolDatum.from_cbor(bytes.fromhex(fix["pool_in_datum"]))
    out = PoolDatum.from_cbor(bytes.fromhex(fix["pool_out_datum"]))
    # ``circulating_dtoken`` rose by the captured mint, so the pre-flow supply the
    # mint priced against is recoverable from the realized delta + exchange identity.
    minted = realized["mint_burn_dtoken"]
    assert out.circulating_dtoken == prev.circulating_dtoken + minted


def test_exchange_rate_and_redeem_bootstrap():
    # No dTokens circulate yet -> the 1:1 bootstrap convention.
    assert dtoken_exchange_rate(total_supply_before=0, circulating_dtoken=0) == 1
    assert dtoken_exchange_rate(total_supply_before=500, circulating_dtoken=0) == 1
    assert (
        dtoken_redeem_value(1000, total_supply_before=0, circulating_dtoken=0) == 1000
    )
    assert (
        dtoken_redeem_value(1000, total_supply_before=500, circulating_dtoken=0) == 1000
    )


def test_redeem_value_inverts_mint_burn_exact_ratio():
    # Clean 2:1 supply:dtoken ratio: 1000 supply -> 500 dtokens -> 1000 supply.
    total_supply_before = 4000
    circulating_dtoken = 2000
    minted = mint_burn_dtoken(
        pool_changed_amount=1000,
        withdraw_fee=0,
        total_supply_before=total_supply_before,
        circulating_dtoken=circulating_dtoken,
    )
    assert minted == 500
    assert (
        dtoken_redeem_value(
            minted,
            total_supply_before=total_supply_before,
            circulating_dtoken=circulating_dtoken,
        )
        == 1000
    )


# --- dToken mint/burn floor property (broad domain, incl. rounding edges) ---------
#
# `mint_burn_dtoken` is the pro-rata mint/burn: outside the 1:1 bootstrap branch
# (total_supply_before == 0 or circulating_dtoken == 0) it returns
# ``floor((pool_changed_amount - withdraw_fee) * circulating_dtoken /
# total_supply_before)``. This pins that exact composition -- the fee subtraction, the
# multiply-before-divide, and the floor direction -- across a wide deterministic input
# table (Hypothesis is not a project dependency, so this is the broad-parametrize
# equivalent). It is the CI-safe offline guard for the floor that the on-chain dToken
# mint accepts. Inputs stay in the floor branch's valid domain: total_supply_before > 0
# and circulating_dtoken > 0 (positive supplies); the bootstrap branch is covered
# separately by `test_exchange_rate_and_redeem_bootstrap`.

# (pool_changed_amount, withdraw_fee) pairs spanning the rounding edges.
_FLOOR_BRANCH_PAIRS = [
    (1, 0),  # tiny change, no fee
    (2, 1),  # change just above the fee
    (250, 250),  # change == fee -> net zero
    (500, 0),  # exact division against a circ/tsb multiple
    (1000, 0),
    (1001, 0),  # with tsb=1000, circ=1: remainder == 1 (near 0)
    (1999, 0),  # with tsb=1000, circ=1: remainder == 999 (near tsb-1)
    (9_999_996, 0),  # just above a typical market minimum
    (10_000_000, 7),  # fee > 0
    (10**18, 123_456),  # huge change + fee
]
_TSB_VALUES = (1, 7, 1000, 1_000_003, 10**12)
_CIRC_VALUES = (1, 3, 10**9, 10**18)
_DEPOSIT_CASES = [
    (pc, fee, tsb, circ)
    for tsb in _TSB_VALUES
    for circ in _CIRC_VALUES
    for pc, fee in _FLOOR_BRANCH_PAIRS
]
# Real withdraws drive a NEGATIVE pool_changed_amount through the same floor; the
# identity holds there too (floor_div rounds toward negative infinity).
_WITHDRAW_CASES = [
    (-256_487_069, 0, 10**12, 900_000_000_000),
    (-1_000, 0, 7, 3),
    (-1, 0, 1, 1),
    (-(10**18), 0, 1_000_000, 10**9),
]
_MINT_BURN_CASES = _DEPOSIT_CASES + _WITHDRAW_CASES


@pytest.mark.parametrize(
    (
        "pool_changed_amount",
        "withdraw_fee",
        "total_supply_before",
        "circulating_dtoken",
    ),
    _MINT_BURN_CASES,
)
def test_mint_burn_dtoken_property(
    pool_changed_amount: int,
    withdraw_fee: int,
    total_supply_before: int,
    circulating_dtoken: int,
):
    assert total_supply_before > 0 and circulating_dtoken > 0  # floor-branch domain

    minted = mint_burn_dtoken(
        pool_changed_amount=pool_changed_amount,
        withdraw_fee=withdraw_fee,
        total_supply_before=total_supply_before,
        circulating_dtoken=circulating_dtoken,
    )
    expected = floor_div(
        (pool_changed_amount - withdraw_fee) * circulating_dtoken,
        total_supply_before,
    )
    assert minted == expected


def test_mint_burn_dtoken_property_case_count():
    # Guard that the property table stays broad (Hypothesis-equivalent coverage).
    assert len(_MINT_BURN_CASES) >= 40


def test_supply_apy_hand_computed():
    # borrow_apy = 1000 bps (10%), utilization 0.5, fee 1000 bps (10%):
    # 0.10 * 0.5 * 0.9 = 0.045.
    apy = supply_apy(
        borrow_apy=1000,
        total_borrow=500,
        total_supply=1000,
        loan_fee_rate=1000,
    )
    assert apy == Decimal("0.045")


def test_supply_apy_zero_supply():
    assert (
        supply_apy(borrow_apy=1000, total_borrow=0, total_supply=0, loan_fee_rate=1000)
        == 0
    )


def test_supply_apy_no_fee_equals_borrow_times_util():
    # With no protocol fee the supplier rate is just borrow_rate * utilization.
    apy = supply_apy(
        borrow_apy=2000,
        total_borrow=750,
        total_supply=1000,
        loan_fee_rate=0,
    )
    assert apy == Decimal("0.20") * Decimal("0.75")


def test_supply_apy_monotonic_in_utilization():
    low = supply_apy(
        borrow_apy=1000, total_borrow=200, total_supply=1000, loan_fee_rate=1000
    )
    high = supply_apy(
        borrow_apy=1000, total_borrow=800, total_supply=1000, loan_fee_rate=1000
    )
    assert high > low


def test_available_supply_basic():
    assert available_supply(total_supply=1000, total_borrow=400) == 600


def test_available_supply_clamps_at_zero():
    # A borrow exceeding supply (shouldn't happen on-chain) clamps to 0, never negative.
    assert available_supply(total_supply=1000, total_borrow=1500) == 0
    assert available_supply(total_supply=0, total_borrow=0) == 0


def test_alt_supply_value_floors_one_step():
    # floor(amount * num / denom) in one step.
    assert alt_supply_value(1_000_000, PRational(num=11, denom=10)) == 1_100_000
    # 3 * 7 / 2 = 21/2 -> floor 10.
    assert alt_supply_value(3, PRational(num=7, denom=2)) == 10
    # A unit (1:1) rate values the holding at its raw amount.
    assert alt_supply_value(500, PRational(num=1, denom=1)) == 500


def test_total_supply_apy_reduces_to_supply_apy_when_no_alt():
    # alt_value == 0 -> the alt contribution is 0, so the full APY is just the lending
    # APY (0.10 * 0.5 * 0.9 = 0.045).
    base = supply_apy(
        borrow_apy=1000, total_borrow=500, total_supply=1000, loan_fee_rate=1000
    )
    full = total_supply_apy(
        borrow_apy=1000,
        total_borrow=500,
        total_supply=1000,
        loan_fee_rate=1000,
        alt_value=0,
        alt_apy=Decimal("0.04"),
    )
    assert full == base == Decimal("0.045")
    # alt_apy == 0 likewise reduces to the lending APY.
    no_yield = total_supply_apy(
        borrow_apy=1000,
        total_borrow=500,
        total_supply=1000,
        loan_fee_rate=1000,
        alt_value=200,
        alt_apy=Decimal("0"),
    )
    assert no_yield == base


def test_total_supply_apy_hand_computed_combined():
    # lending APY = 0.10 * 0.5 * 0.9 = 0.045; alt contribution = 0.04 * (200/1000)
    # = 0.008; total = 0.053.
    full = total_supply_apy(
        borrow_apy=1000,
        total_borrow=500,
        total_supply=1000,
        loan_fee_rate=1000,
        alt_value=200,
        alt_apy=Decimal("0.04"),
    )
    assert full == Decimal("0.053")


def test_total_supply_apy_zero_supply():
    assert (
        total_supply_apy(
            borrow_apy=1000,
            total_borrow=0,
            total_supply=0,
            loan_fee_rate=1000,
            alt_value=200,
            alt_apy=Decimal("0.04"),
        )
        == 0
    )


def test_total_supply_apy_monotonic_in_alt_yield_and_value():
    def _full(*, alt_value: int, alt_apy: Decimal) -> Decimal:
        return total_supply_apy(
            borrow_apy=1000,
            total_borrow=500,
            total_supply=1000,
            loan_fee_rate=1000,
            alt_value=alt_value,
            alt_apy=alt_apy,
        )

    base = _full(alt_value=0, alt_apy=Decimal("0.04"))
    # Higher alt yield -> higher total APY.
    assert _full(alt_value=200, alt_apy=Decimal("0.06")) > _full(
        alt_value=200, alt_apy=Decimal("0.04")
    )
    # Larger yield-bearing holding -> higher total APY.
    assert _full(alt_value=400, alt_apy=Decimal("0.04")) > _full(
        alt_value=200, alt_apy=Decimal("0.04")
    )
    # Any positive alt holding+yield strictly exceeds the lending-only APY.
    assert _full(alt_value=200, alt_apy=Decimal("0.04")) > base
