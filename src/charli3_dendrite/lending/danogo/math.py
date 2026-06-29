"""Danogo exact-integer math mirroring the on-chain validators."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

from charli3_dendrite.lending.math import BASIS
from charli3_dendrite.lending.math import floor_div

if TYPE_CHECKING:
    from charli3_dendrite.lending.danogo.datums import PRational

YEAR_IN_MS = 31_536_000_000


def current_interest_index(
    interest_index: int,
    *,
    borrow_apy: int,
    interest_time: int,
    txn_time: int,
) -> int:
    """floor(ii + ii*apy/basis * (txn_time-interest_time)/year_in_ms)."""
    if txn_time <= interest_time:
        return interest_index
    growth = floor_div(
        interest_index * borrow_apy * (txn_time - interest_time),
        BASIS * YEAR_IN_MS,
    )
    return interest_index + growth


def current_loan_amount(
    *,
    loan_amount: int,
    current_index: int,
    initial_index: int,
) -> int:
    """floor(loan_amount * current_index / initial_index)."""
    return floor_div(loan_amount * current_index, initial_index)


def total_collateral_val(terms: list[tuple[int, int, int]]) -> int:
    """Sum of floor(amount * num/denom) over (amount, num, denom) terms."""
    return sum(floor_div(amount * num, denom) for amount, num, denom in terms)


def total_collateral_val_with_threshold(terms: list[tuple[int, int, int, int]]) -> int:
    """Sum of floor(amount * num/denom * threshold_bps/basis).

    NOTE: this floors ``amount*num*threshold / (denom*BASIS)`` in ONE step. The
    on-chain validator's convention must match — specifically whether it instead
    floors the collateral value first and then applies the threshold (two-step).
    This term sits in the health-factor numerator, and the one-step form is the
    more generous one at boundaries.
    """
    return sum(
        floor_div(amount * num * threshold, denom * BASIS)
        for amount, num, denom, threshold in terms
    )


def util_rate(*, total_borrow: int, total_supply: int) -> Decimal:
    """total_borrow / total_supply (0 when supply is 0)."""
    if total_supply <= 0:
        return Decimal(0)
    return Decimal(total_borrow) / Decimal(total_supply)


def current_borrow_apy(
    *,
    power_base: int,
    base_rate: int,
    total_borrow: int,
    total_supply: int,
) -> int:
    """floor((power_base/basis)^round(util*100) * 100) + base_rate.

    `util*100` is rounded to the nearest integer exponent. When total_supply is 0,
    returns base_rate (matches Create Pool / zero-supply rule).

    NOTE: the exponent uses HALF_UP rounding of ``util*100``. The validator's
    rounding/quantization convention for the exponent and the ``power_base`` value
    must match the on-chain pool.
    """
    if total_supply <= 0:
        return base_rate
    util = util_rate(total_borrow=total_borrow, total_supply=total_supply)
    exponent = int((util * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    num = power_base**exponent
    denom = BASIS**exponent
    curve = floor_div(num * 100, denom)
    return curve + base_rate


def dtoken_exchange_rate(
    *,
    total_supply_before: int,
    circulating_dtoken: int,
) -> Fraction:
    """Supply tokens per dToken: ``total_supply_before / circulating_dtoken``.

    This is the rate at which a dToken redeems into the underlying supply token.
    There is no per-dToken interest field; supplier yield accrues entirely through
    this exchange rate rising as borrowers pay interest into ``total_supply``.

    ``total_supply_before`` is the post-accrual, pre-flow supply (the same quantity
    `mint_burn_dtoken` / `synth_pool_datum_topup_withdraw` mint against), so the
    caller supplies it; this helper stays a pure ratio so it is trivially correct
    and testable.

    On bootstrap -- when no dTokens circulate -- the rate is the 1:1 convention
    `mint_burn_dtoken` uses (it mints the deposited supply 1:1), so ``Fraction(1)``
    is returned.
    """
    if circulating_dtoken == 0:
        return Fraction(1)
    return Fraction(total_supply_before, circulating_dtoken)


def dtoken_redeem_value(
    dtokens: int,
    *,
    total_supply_before: int,
    circulating_dtoken: int,
) -> int:
    """Supply tokens returned for redeeming ``dtokens``.

    ``floor(dtokens * total_supply_before / circulating_dtoken)`` -- the inverse of
    `mint_burn_dtoken` (which floors ``Δsupply * circulating_dtoken /
    total_supply_before``). ``total_supply_before`` is the post-accrual, pre-flow
    supply, matching `mint_burn_dtoken`.

    On bootstrap -- when no dTokens circulate -- the 1:1 convention applies and the
    redeem value equals ``dtokens``.
    """
    if circulating_dtoken == 0:
        return dtokens
    return floor_div(dtokens * total_supply_before, circulating_dtoken)


def supply_apy(
    *,
    borrow_apy: int,
    total_borrow: int,
    total_supply: int,
    loan_fee_rate: int,
) -> Decimal:
    """Suppliers' annualized rate (the dToken yield), as a Decimal fraction.

    Returns a fraction, e.g. ``Decimal("0.0123")`` for 1.23%; multiply by 100 for a
    percentage.

    Derived from the accrual identity, not guessed. Over a year the pool's accounted
    supply grows by ``(accumulated - fee) / total_supply`` where:

    - ``accumulated ~= total_borrow * borrow_apy / BASIS`` is the interest accrued on
      the outstanding borrow (``borrow_apy`` is the datum's basis-point borrow rate,
      so ``borrow_apy / BASIS`` is the per-year fractional borrow rate), and
    - ``fee = accumulated * loan_fee_rate / BASIS`` is the protocol's cut.

    So::

        supply_apy = (borrow_apy / BASIS)
                     * (total_borrow / total_supply)
                     * (1 - loan_fee_rate / BASIS)

    i.e. the borrow rate scaled by utilization and net of the protocol fee. Returns
    ``Decimal(0)`` when ``total_supply <= 0`` (no suppliers earn anything).

    EXCLUDES alt-supply-token yield: staked-ADA (and similar) pools earn an extra,
    oracle-driven yield by re-pricing their alternative supply holdings (the
    ``alt_tokens_interest`` term the accrual books). That is not captured here, so
    for alt-supply pools this UNDERSTATES the true supplier APY.
    """
    if total_supply <= 0:
        return Decimal(0)
    borrow_rate = Decimal(borrow_apy) / Decimal(BASIS)
    utilization = Decimal(total_borrow) / Decimal(total_supply)
    fee_retained = Decimal(BASIS - loan_fee_rate) / Decimal(BASIS)
    return borrow_rate * utilization * fee_retained


def available_supply(*, total_supply: int, total_borrow: int) -> int:
    """Un-borrowed supply liquidity: ``max(total_supply - total_borrow, 0)``.

    This is the accounted supply that is NOT currently lent out -- the amount that can
    still be withdrawn by suppliers or drawn by new borrowers, clamped at 0 (a pool is
    never borrowed beyond its supply, but clamp defensively).

    It is an ACCOUNTING figure derived from the pool datum, NOT the pool UTxO's on-hand
    lovelace. For an alt-supply pool the on-hand lovelace also depends on the pool's
    alternative-supply-token holdings (whose value is booked into ``total_supply`` via
    the oracle revaluation), so the two differ; use `alt_supply_value` to value those
    holdings.
    """
    return max(total_supply - total_borrow, 0)


def alt_supply_value(amount: int, rate: PRational) -> int:
    """ADA (lovelace) value of an alt-supply holding: ``floor(amount * num / denom)``.

    ``rate`` is the pool datum's per-token ``PRational`` (ADA lovelace per alt token,
    the ``alt_supply_tokens_rate`` entry). Floors in one step, matching the on-chain
    ``alt_tokens_interest`` revaluation convention (`_alt_supply_update`).
    """
    return floor_div(amount * rate.num, rate.denom)


def total_supply_apy(
    *,
    borrow_apy: int,
    total_borrow: int,
    total_supply: int,
    loan_fee_rate: int,
    alt_value: int,
    alt_apy: Decimal,
) -> Decimal:
    """Full supplier APY including alt-supply-token yield, as a Decimal fraction.

    Suppliers in an alt-supply (e.g. staked-ADA) market earn TWO returns, both
    accounted into ``total_supply``:

    - the lending yield -- borrow interest (net of the protocol fee) on the borrowed
      fraction -- captured by `supply_apy`, and
    - the appreciation of the pool's yield-bearing alt holdings: the pool re-prices
      those holdings from the oracle and books the value change as supplier interest
      (the ``alt_tokens_interest`` term). Over a year that contributes
      ``alt_apy * alt_value`` lovelace, i.e. ``alt_apy * (alt_value / total_supply)``
      as a fraction of the supplied base.

    So::

        total_supply_apy = supply_apy(...) + alt_apy * (alt_value / total_supply)

    This composes `supply_apy` ADDITIVELY -- the lending APY is unchanged; this adds
    the alt-yield contribution on top. ``alt_value`` is the ADA (lovelace) value of the
    pool's yield-bearing alt holdings (`alt_supply_value`) and ``alt_apy`` is the alt
    token's annual yield fraction (derived empirically from its oracle rate over time).

    Returns ``Decimal(0)`` when ``total_supply <= 0``; reduces to exactly `supply_apy`
    when ``alt_value`` or ``alt_apy`` is 0 (no alt holdings / no alt yield).
    """
    lending = supply_apy(
        borrow_apy=borrow_apy,
        total_borrow=total_borrow,
        total_supply=total_supply,
        loan_fee_rate=loan_fee_rate,
    )
    if total_supply <= 0:
        return Decimal(0)
    alt_contribution = alt_apy * (Decimal(alt_value) / Decimal(total_supply))
    return lending + alt_contribution
