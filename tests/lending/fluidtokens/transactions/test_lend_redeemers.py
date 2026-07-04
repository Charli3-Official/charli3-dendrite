"""Request Lend action + withdraw redeemer round-trip with the right constructors."""

from __future__ import annotations

from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    RequestCancelAction,
    RequestLendAction,
    RequestWithdrawRedeemer,
)
from pycardano import IndefiniteList
from pycardano import RawPlutusData


def test_request_lend_action_is_constr2() -> None:
    action = RequestLendAction(
        principal_oracle_ref_input_index=0,
        collateral_oracle_ref_input_index=0,
        given_principal_amount=10_000_000_000,
        request_id=b"\x00abc",
        permissioned_condition_withdraw_index=0,
    )
    decoded = RawPlutusData.from_cbor(action.to_cbor())
    assert decoded.data.tag == 123  # Constr2 (Cancel=Constr0, CancelAfterExp=Constr1)
    assert RequestLendAction.from_cbor(action.to_cbor()) == action


def test_withdraw_redeemer_coerces_lend_action_and_round_trips() -> None:
    action = RequestLendAction(
        principal_oracle_ref_input_index=0,
        collateral_oracle_ref_input_index=0,
        given_principal_amount=10_000_000_000,
        request_id=b"\x00abc",
        permissioned_condition_withdraw_index=0,
    )
    rdmr = RequestWithdrawRedeemer(
        config_ref_input_index=3,
        actions_for_each_input=IndefiniteList([action]),
    )
    reparsed = RequestWithdrawRedeemer.from_cbor(rdmr.to_cbor())
    assert isinstance(reparsed.actions_for_each_input.data[0], RequestLendAction)
    assert reparsed.to_cbor() == rdmr.to_cbor()


def test_withdraw_redeemer_still_coerces_cancel_action() -> None:
    rdmr = RequestWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=IndefiniteList(
            [RequestCancelAction(request_id=b"\x00x")]
        ),
    )
    reparsed = RequestWithdrawRedeemer.from_cbor(rdmr.to_cbor())
    assert isinstance(reparsed.actions_for_each_input.data[0], RequestCancelAction)
    assert reparsed.to_cbor() == rdmr.to_cbor()
