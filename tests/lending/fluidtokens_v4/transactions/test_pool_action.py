"""What every V4 pool edit and cancel checks before it touches the builder."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSpendScript
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import owner_pkh
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    pool_address,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    pool_manager_address,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    require_paired_order,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    require_sole_pool_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    PoolCancelSnapshot,
)
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import second_pool


def _position() -> PoolPosition:
    (position,) = PoolCancelSnapshot.from_capture(fixture("pool_cancel")).positions
    return position


def test_a_captured_position_is_managed_and_owned_by_a_key() -> None:
    position = _position()
    position.check()
    assert position.owner_pkh.hex().startswith("1db7e8e3")
    assert position.pool_id.hex().startswith("0024af38")


def test_a_pool_not_authorised_by_its_manager_is_refused() -> None:
    position = _position()
    datum = position.pool_datum
    datum.lender_auth = AuthCardanoSignature(key_hash=position.owner_pkh)
    position.pool = replace(position.pool, datum=datum.to_cbor_hex())
    with pytest.raises(NotImplementedError, match="not authorised by its pool manager"):
        position.check()


def test_a_manager_of_another_pool_is_refused() -> None:
    position = _position()
    other = second_pool(position, pool_ref=("aa" * 32, 0), manager_ref=("bb" * 32, 0))
    with pytest.raises(ValueError, match="does not manage pool"):
        PoolPosition(pool=position.pool, pool_manager=other.pool_manager).check()


def test_an_owner_that_is_not_a_key_is_refused() -> None:
    manager = PoolManagerDatum(
        pool_owner_auth=AuthCardanoSpendScript(script_hash=b"\x01" * 28),
        compounding_fee_per_mille=5,
    )
    with pytest.raises(NotImplementedError, match="owned by a key"):
        owner_pkh(manager)


def test_pools_and_managers_must_sort_alike() -> None:
    position = _position()
    # The captured pool sorts at 69dd..., its manager at 5116....
    after = second_pool(position, pool_ref=("70" * 32, 0), manager_ref=("52" * 32, 0))
    require_paired_order([position, after])
    before = second_pool(position, pool_ref=("00" * 32, 0), manager_ref=("52" * 32, 0))
    with pytest.raises(ValueError, match="separate transactions"):
        require_paired_order([before, position])


def _builder() -> TransactionBuilder:
    return TransactionBuilder(EvalContext(last_block_slot=0))


def test_a_builder_without_a_pool_action_passes() -> None:
    require_sole_pool_action(_builder())


def test_a_builder_holding_a_pool_action_is_refused() -> None:
    wallet = Address.decode(fixture("pool_cancel")["outputs"][0]["address"])
    for script in (pool_address(str(wallet)), pool_manager_address(str(wallet))):
        tx_builder = _builder()
        tx_builder.add_output(TransactionOutput(script, 2_000_000))
        with pytest.raises(ValueError, match="only pool action"):
            require_sole_pool_action(tx_builder)
    tx_builder = _builder()
    tx_builder.withdrawals = {reward_address(c.POOL_POLICY): 0}  # type: ignore[assignment]
    with pytest.raises(ValueError, match="only pool action"):
        require_sole_pool_action(tx_builder)


def test_script_addresses_carry_the_lender_stake() -> None:
    # The cancelled pool and its manager sat under the lender wallet's stake key.
    position = _position()
    wallet = fixture("pool_cancel")["outputs"][0]["address"]
    assert str(pool_address(wallet)) == position.pool.address
    assert str(pool_manager_address(wallet)) == position.pool_manager.address
