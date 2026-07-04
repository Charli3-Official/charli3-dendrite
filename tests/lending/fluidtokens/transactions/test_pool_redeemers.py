"""Pool create/cancel redeemers round-trip and use the right constructor ids."""

from __future__ import annotations

from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import PoolCancelAction
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import (
    PoolCancelWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens.transactions.redeemers import PoolMintRedeemer
from pycardano import IndefiniteList
from pycardano import RawPlutusData


def test_pool_mint_redeemer_is_constr0_with_outref() -> None:
    rdmr = PoolMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(tx_id=bytes(32), index=3),
    )
    decoded = RawPlutusData.from_cbor(rdmr.to_cbor())
    assert decoded.data.tag == 121  # Constr0
    assert PoolMintRedeemer.from_cbor(rdmr.to_cbor()) == rdmr


def test_pool_cancel_action_is_constr0_with_pool_id() -> None:
    action = PoolCancelAction(pool_id=b"\x00abc")
    decoded = RawPlutusData.from_cbor(action.to_cbor())
    assert decoded.data.tag == 121  # Constr0 (Borrow is Constr1)
    assert PoolCancelAction.from_cbor(action.to_cbor()) == action


def test_pool_cancel_withdraw_coerces_elements_to_cancel_action() -> None:
    rdmr = PoolCancelWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=IndefiniteList([PoolCancelAction(pool_id=b"\x00abc")]),
    )
    reparsed = PoolCancelWithdrawRedeemer.from_cbor(rdmr.to_cbor())
    assert isinstance(reparsed.actions_for_each_input.data[0], PoolCancelAction)
    assert reparsed.to_cbor() == rdmr.to_cbor()
