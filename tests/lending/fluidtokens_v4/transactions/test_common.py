"""V4 transaction plumbing: loan addresses and withdrawals."""

from __future__ import annotations

from pycardano import Address
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    add_zero_withdrawals,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import loan_address
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import fixture


def test_loan_address_carries_the_borrower_stake() -> None:
    for name in ("borrow_single", "borrow_multi"):
        fix = fixture(name)
        loans = [
            u
            for u in fix["outputs"]
            if any(p == c.LOAN_POLICY for p, _, _ in u["assets"])
        ]
        wallet = next(u["address"] for u in fix["inputs"] if not u.get("datum"))
        assert loans
        assert {u["address"] for u in loans} == {loan_address(wallet)}


def test_loan_address_of_an_enterprise_wallet_has_no_stake() -> None:
    fix = fixture("borrow_single")
    base = Address.decode(
        next(u["address"] for u in fix["inputs"] if not u.get("datum"))
    )
    wallet = Address(payment_part=base.payment_part, network=base.network).encode()
    loan = Address.decode(loan_address(wallet))
    assert loan.staking_part is None
    assert loan.payment_part.payload.hex() == c.LOAN_SPEND_SKH


def test_zero_withdrawals_merge_with_existing_ones() -> None:
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=0))
    add_zero_withdrawals(tx_builder, [c.POOL_POLICY])
    add_zero_withdrawals(tx_builder, [c.LOAN_POLICY])
    assert dict(tx_builder.withdrawals) == {
        reward_address(c.POOL_POLICY): 0,
        reward_address(c.LOAN_POLICY): 0,
    }


def test_script_hash_of_a_reference_script() -> None:
    refs = [utxo_from_dict(u) for u in fixture("borrow_single")["ref_inputs"]]
    hashes = {script_hash_of(u) for u in refs if u.ref_script}
    assert {c.POOL_SPEND_SKH, c.POOL_POLICY, c.POOL_BORROW_ACTION_SKH} <= hashes
