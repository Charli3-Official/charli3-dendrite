"""Cardano-Swaps beacon minting policy: registered once per transaction.

Several creates or closes composed on one ``TransactionBuilder`` share one
beacon policy run. The policy checks every beacon output in the transaction, so
the builder must carry exactly one minting-script redeemer per beacon policy,
while the mint field accumulates every call's beacons.
"""

from __future__ import annotations

from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import script_hash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderState
from charli3_dendrite.dexs.ob.cardanoswaps import CreateOrCloseSwaps
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithMint
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import CardanoSwapsTwoWayOrderState
from tests.test_cardanoswaps import OWNER
from tests.test_cardanoswaps import TOKEN_A_NAME
from tests.test_cardanoswaps import TOKEN_A_POLICY
from tests.test_cardanoswaps import TOKEN_A_UNIT
from tests.test_cardanoswaps import _make_datum
from tests.test_cardanoswaps import _mint_dict
from tests.test_cardanoswaps import _OfflineContext
from tests.test_cardanoswaps import _redeemers
from tests.test_cardanoswaps import _ref_utxo
from tests.test_cardanoswaps import _utxo_assets
from tests.test_cardanoswaps import _values_from_datum
from tests.test_cardanoswaps_two_way import _create
from tests.test_cardanoswaps_two_way import _state
from tests.test_cardanoswaps_two_way_contracts import USDM_UNIT
from tests.test_cardanoswaps_two_way_contracts import vector_datum

N = 5


def _builder() -> TransactionBuilder:
    return TransactionBuilder(_OfflineContext())


def _mint_redeemers_for(tx_builder: TransactionBuilder, policy_id: str) -> list:
    """The builder's minting-script redeemers whose script hashes to ``policy_id``."""
    policy = ScriptHash(bytes.fromhex(policy_id))
    return [
        redeemer
        for script, redeemer in tx_builder._minting_script_to_redeemers
        if script_hash(script) == policy
    ]


def _assert_one_beacon_redeemer(tx_builder: TransactionBuilder, policy_id: str) -> None:
    (redeemer,) = list(tx_builder._minting_script_to_redeemers)
    assert isinstance(redeemer[1].data, CreateOrCloseSwaps)
    assert len(_mint_redeemers_for(tx_builder, policy_id)) == 1


def _one_way_create(tx_builder: TransactionBuilder, **kwargs) -> None:
    CardanoSwapsOrderState.build_create(
        owner_address=OWNER,
        offer=Assets(root={"lovelace": 10_000_000}),
        ask=Assets(root={TOKEN_A_UNIT: 0}),
        price=(2, 1),
        tx_builder=tx_builder,
        **kwargs,
    )


def _one_way_resting(tx_index: int) -> CardanoSwapsOrderState:
    """A resting ADA -> TOKEN_A one-way swap at output ``tx_index``."""
    datum = _make_datum(
        b"",
        b"",
        bytes.fromhex(TOKEN_A_POLICY),
        bytes.fromhex(TOKEN_A_NAME),
        2,
        1,
    )
    values = _values_from_datum(datum, _utxo_assets(datum, {"lovelace": 10_000_000}))
    values["tx_index"] = tx_index
    return CardanoSwapsOrderState.model_validate(values)


def _two_way_resting(tx_index: int) -> CardanoSwapsTwoWayOrderState:
    """A resting ADA/USDM two-way swap at output ``tx_index``."""
    return _state(
        vector_datum(),
        {"lovelace": 10_000_000, USDM_UNIT: 30},
        tx_index=tx_index,
    )


# --- two-way ------------------------------------------------------------------


def test_two_way_creates_share_one_beacon_redeemer() -> None:
    tx_builder = _builder()
    datum = None
    for _ in range(N):
        _, datum = _create(tx_builder)
    assert datum is not None
    _assert_one_beacon_redeemer(tx_builder, TWO_WAY_BEACON_POLICY_ID)
    assert _mint_dict(tx_builder) == {unit: N for unit in datum.beacon_assets(1).root}
    assert len(tx_builder.outputs) == N


def test_two_way_creates_with_a_beacon_reference_share_one_redeemer() -> None:
    tx_builder = _builder()
    beacon_ref = _ref_utxo(CardanoSwapsTwoWayOrderState._beacon_script())
    for _ in range(N):
        _create(tx_builder, beacon_ref_utxo=beacon_ref)
    _assert_one_beacon_redeemer(tx_builder, TWO_WAY_BEACON_POLICY_ID)
    assert tx_builder.reference_inputs == {beacon_ref}
    assert tx_builder.scripts == []
    assert all(qty == N for qty in _mint_dict(tx_builder).values())


def test_two_way_closes_share_one_beacon_redeemer() -> None:
    tx_builder = _builder()
    states = [_two_way_resting(i) for i in range(N)]
    for state in states:
        state.build_close(tx_builder=tx_builder, owner_address=OWNER)
    _assert_one_beacon_redeemer(tx_builder, TWO_WAY_BEACON_POLICY_ID)
    beacons = states[0].order_datum.beacon_assets(1).root
    assert _mint_dict(tx_builder) == {unit: -N for unit in beacons}
    assert [type(r.data) for r in _redeemers(tx_builder)] == [SpendWithMint] * N


# --- one-way ------------------------------------------------------------------


def test_one_way_creates_share_one_beacon_redeemer() -> None:
    tx_builder = _builder()
    for _ in range(N):
        _one_way_create(tx_builder)
    _assert_one_beacon_redeemer(tx_builder, BEACON_POLICY_ID)
    mint = _mint_dict(tx_builder)
    assert len(mint) == 3
    assert all(unit.startswith(BEACON_POLICY_ID) for unit in mint)
    assert all(qty == N for qty in mint.values())
    assert len(tx_builder.outputs) == N


def test_one_way_closes_share_one_beacon_redeemer() -> None:
    tx_builder = _builder()
    swap_ref = _ref_utxo(CardanoSwapsOrderState._swap_script(), index=0)
    beacon_ref = _ref_utxo(CardanoSwapsOrderState._beacon_script(), index=1)
    for i in range(N):
        _one_way_resting(i).build_close(
            tx_builder=tx_builder,
            owner_address=OWNER,
            swap_ref_utxo=swap_ref,
            beacon_ref_utxo=beacon_ref,
        )
    _assert_one_beacon_redeemer(tx_builder, BEACON_POLICY_ID)
    mint = _mint_dict(tx_builder)
    assert len(mint) == 3
    assert all(qty == -N for qty in mint.values())
    assert [type(r.data) for r in _redeemers(tx_builder)] == [SpendWithMint] * N
    assert tx_builder.reference_inputs == {swap_ref, beacon_ref}


# --- distinct policies ----------------------------------------------------------


def test_each_beacon_policy_is_registered_once() -> None:
    """One-way and two-way swaps in one transaction run their own policies."""
    tx_builder = _builder()
    for _ in range(N):
        _one_way_create(tx_builder)
        _create(tx_builder)
    assert len(tx_builder._minting_script_to_redeemers) == 2
    assert len(_mint_redeemers_for(tx_builder, BEACON_POLICY_ID)) == 1
    assert len(_mint_redeemers_for(tx_builder, TWO_WAY_BEACON_POLICY_ID)) == 1
    assert all(abs(qty) == N for qty in _mint_dict(tx_builder).values())
