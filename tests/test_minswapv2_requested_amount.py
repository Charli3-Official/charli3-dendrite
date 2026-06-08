"""Regression tests for ``MinswapV2OrderDatum.requested_amount()`` on the step
types that previously fell through to the empty ``Assets({})`` else branch.

StopLossV2 / OCOV2 / ZapOutV2 / PartialSwapV2 / WithdrawImbalanceV2 are valid
on-chain MinswapV2 order steps, but ``requested_amount()`` returned nothing for
them, so consumers could not recover the order's ask. These tests pin the ask
each now reports.
"""

from pycardano import Address, VerificationKeyHash

from charli3_dendrite.dexs.amm.minswap import (
    BoolFalse,
    BoolTrue,
    MinswapV2OrderDatum,
    OCOV2,
    PartialSwapV2,
    SAOSpecificAmount,
    StopLossV2,
    WithdrawImbalanceV2,
    ZapOutV2,
)
from charli3_dendrite.utility import Assets

_ADDR = Address(
    payment_part=VerificationKeyHash(bytes(28)),
    staking_part=VerificationKeyHash(bytes(28)),
)
_IN = Assets(**{"aa" * 28: 1_000_000})
_OUT = Assets(**{"bb" * 28: 500_000})


def _datum_with(step) -> MinswapV2OrderDatum:
    """A valid MinswapV2OrderDatum carrying ``step`` (other fields are dummies)."""
    datum = MinswapV2OrderDatum.create_datum(
        address_source=_ADDR,
        in_assets=_IN,
        out_assets=_OUT,
        batcher_fee=Assets(lovelace=2_000_000),
        deposit=Assets(lovelace=2_000_000),
    )
    datum.step = step
    return datum


def test_stop_loss_uses_stop_loss_receive():
    step = StopLossV2(
        a_to_b_direction=BoolTrue(),
        swap_amount_option=SAOSpecificAmount(swap_amount=1000),
        stop_loss_receive=900,
    )
    assert _datum_with(step).requested_amount() == Assets({"asset_b": 900})


def test_oco_uses_minimum_receive_not_stop_loss():
    step = OCOV2(
        a_to_b_direction=BoolFalse(),
        swap_amount_option=SAOSpecificAmount(swap_amount=1000),
        minimum_receive=950,
        stop_loss_receive=800,
    )
    assert _datum_with(step).requested_amount() == Assets({"asset_a": 950})


def test_zap_out_uses_minimum_receive():
    step = ZapOutV2(
        a_to_b_direction=BoolTrue(),
        withdrawal_amount_option=0,
        minimum_receive=700,
        killable=BoolFalse(),
    )
    assert _datum_with(step).requested_amount() == Assets({"asset_b": 700})


def test_partial_swap_computes_from_io_ratio():
    # 1000 * 9 // 10 == 900
    step = PartialSwapV2(
        a_to_b_direction=BoolTrue(),
        total_swap_amount=1000,
        io_ratio_numerator=9,
        io_ratio_denominator=10,
        hops=1,
        minimum_swap_amount_required=100,
        max_batcher_fee_each_time=1000,
    )
    assert _datum_with(step).requested_amount() == Assets({"asset_b": 900})


def test_withdraw_imbalance_uses_minimum_asset_a():
    step = WithdrawImbalanceV2(
        withdrawal_amount_optino=0,
        ratio_asset_a=1,
        ratio_asset_b=1,
        minimum_asset_a=600,
        killable=BoolFalse(),
    )
    assert _datum_with(step).requested_amount() == Assets({"asset_a": 600})
