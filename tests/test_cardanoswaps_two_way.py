"""Cardano-Swaps two-way order state: parse, orient, take and close.

A two-way swap UTxO quotes both directions of its pair. Each
``CardanoSwapsTwoWayOrderState`` is one direction's view of it: ``take_asset2``
selects the direction, and the assets are oriented for the taker (``in`` = the
asset paid, ``out`` = the asset taken).
"""

from __future__ import annotations

import pytest
from pycardano import TransactionBuilder
from pycardano.utils import min_lovelace

from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsRational
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeInt
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRefV3
from charli3_dendrite.dexs.ob.cardanoswaps import CreateOrCloseSwaps
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithMint
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_SWAP_VALIDATOR_HASH
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import CardanoSwapsTwoWayOrderState
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import CardanoSwapsTwoWaySwapDatum
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TakeAsset1
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TakeAsset2
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import two_way_asset_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import two_way_pair_beacon_name
from charli3_dendrite.utility import posix_ms_to_slot
from tests.test_cardanoswaps import OWNER
from tests.test_cardanoswaps import TX_HASH
from tests.test_cardanoswaps import _mint_dict
from tests.test_cardanoswaps import _OfflineContext
from tests.test_cardanoswaps import _output_units
from tests.test_cardanoswaps import _redeemers
from tests.test_cardanoswaps import _ref_utxo
from tests.test_cardanoswaps_contracts import _ContextAt
from tests.test_cardanoswaps_two_way_contracts import USDM_UNIT
from tests.test_cardanoswaps_two_way_contracts import vector_datum
from tests.test_cardanoswaps_two_way_datum import TEST_UNIT
from tests.test_cardanoswaps_two_way_datum import _token_pair_datum

CPUB = _OfflineContext().protocol_param.coins_per_utxo_byte

# A token pair whose two assets share one policy id (asset1 sorts by name).
SAME_POLICY = bytes.fromhex("dd" * 28)
NAME_A = b"A"
NAME_B = b"B"
SAME_A_UNIT = SAME_POLICY.hex() + NAME_A.hex()
SAME_B_UNIT = SAME_POLICY.hex() + NAME_B.hex()

EXPIRATION = 1_790_000_040_000  # a 60 000 ms multiple
FAR_FUTURE = 4_102_444_800_000  # 2100-01-01, a 60 000 ms multiple


@pytest.fixture
def tx_builder() -> TransactionBuilder:
    return TransactionBuilder(_OfflineContext())


def _same_policy_datum() -> CardanoSwapsTwoWaySwapDatum:
    """A two-way swap between two tokens under one policy id."""
    return CardanoSwapsTwoWaySwapDatum(
        beacon_id=bytes.fromhex(TWO_WAY_BEACON_POLICY_ID),
        pair_beacon=two_way_pair_beacon_name(SAME_POLICY, NAME_A, SAME_POLICY, NAME_B),
        asset1_id=SAME_POLICY,
        asset1_name=NAME_A,
        asset1_beacon=two_way_asset_beacon_name(SAME_POLICY, NAME_A),
        asset2_id=SAME_POLICY,
        asset2_name=NAME_B,
        asset2_beacon=two_way_asset_beacon_name(SAME_POLICY, NAME_B),
        asset1_price=CardanoSwapsRational(2, 1),
        asset2_price=CardanoSwapsRational(1, 3),
        prev_input=PlutusNone(),
        expiration=PlutusNone(),
    )


def _state(
    datum: CardanoSwapsTwoWaySwapDatum,
    balance: dict[str, int],
    *,
    take_asset2: bool = False,
    tx_index: int = 0,
) -> CardanoSwapsTwoWayOrderState:
    """Parse one direction of a swap UTxO holding ``balance`` plus its beacons."""
    root = dict(balance)
    root.update(datum.beacon_assets(1).root)
    values = {
        "tx_hash": TX_HASH,
        "tx_index": tx_index,
        "datum_cbor": datum.to_cbor_hex(),
        "datum_hash": datum.hash().to_primitive().hex(),
        "assets": Assets(root=root),
        "block_time": 0,
        "block_index": 0,
        "plutus_v2": False,
        "take_asset2": take_asset2,
    }
    return CardanoSwapsTwoWayOrderState.model_validate(values)


def _ada_pair(
    take_asset2: bool = False, **datum_kwargs
) -> CardanoSwapsTwoWayOrderState:
    """10 ADA and 30 USDM on the ADA/USDM vector datum."""
    return _state(
        vector_datum(**datum_kwargs),
        {"lovelace": 10_000_000, USDM_UNIT: 30},
        take_asset2=take_asset2,
    )


def _token_pair(take_asset2: bool = False) -> CardanoSwapsTwoWayOrderState:
    """1 000 TEST and 1 000 USDM over a 4 ADA carrier."""
    return _state(
        _token_pair_datum(),
        {"lovelace": 4_000_000, TEST_UNIT: 1_000, USDM_UNIT: 1_000},
        take_asset2=take_asset2,
    )


def _held(txo, unit: str) -> int:
    """The quantity of ``unit`` an output holds."""
    if unit == "lovelace":
        return txo.amount.coin
    for policy, names in txo.amount.multi_asset.data.items():
        for name, qty in names.items():
            if bytes(policy).hex() + bytes(name).hex() == unit:
                return qty
    return 0


# --- parse and orientation --------------------------------------------------


def test_parse_ada_pair_take_asset1_orients_in_token_out_ada() -> None:
    state = _ada_pair()
    assert state.take_asset2 is False
    assert state.in_unit == USDM_UNIT
    assert state.out_unit == "lovelace"
    assert state.dex_nft == vector_datum().beacon_assets(1)
    assert all(not unit.startswith(TWO_WAY_BEACON_POLICY_ID) for unit in state.assets)
    assert state.price == (3, 2_000_000)
    assert state.available == Assets(root={"lovelace": 10_000_000})
    assert state.inactive is False


def test_parse_ada_pair_take_asset2_orients_in_ada_out_token() -> None:
    state = _ada_pair(take_asset2=True)
    assert state.take_asset2 is True
    assert state.in_unit == "lovelace"
    assert state.out_unit == USDM_UNIT
    assert state.price == (1_900_000, 3)
    assert state.available == Assets(root={USDM_UNIT: 30})


def test_token_pair_keeps_lovelace_as_residual() -> None:
    take1 = _token_pair()
    assert list(take1.assets) == [USDM_UNIT, TEST_UNIT, "lovelace"]
    assert take1.price == (3, 2)
    take2 = _token_pair(take_asset2=True)
    assert list(take2.assets) == [TEST_UNIT, USDM_UNIT, "lovelace"]
    assert take2.price == (5, 7)
    assert take2.assets["lovelace"] == 4_000_000


def test_opposite_flips_direction_and_keeps_the_utxo() -> None:
    state = _ada_pair()
    state.address = str(state.beacon_address(OWNER))
    other = state.opposite()
    assert isinstance(other, CardanoSwapsTwoWayOrderState)
    assert other.take_asset2 is True
    assert (other.in_unit, other.out_unit) == (state.out_unit, state.in_unit)
    assert (other.tx_hash, other.tx_index) == (state.tx_hash, state.tx_index)
    assert other.datum_cbor == state.datum_cbor
    assert other.address == state.address
    assert other.dex_nft == state.dex_nft
    assert dict(other.assets.root) == {USDM_UNIT: 30, "lovelace": 10_000_000}
    assert other.opposite().take_asset2 is False


def test_directions_are_take_asset1_then_take_asset2() -> None:
    for state in (_ada_pair(), _ada_pair(take_asset2=True)):
        take1, take2 = state.directions()
        assert (take1.take_asset2, take2.take_asset2) == (False, True)
        assert state in (take1, take2)
        assert take1.out_unit == "lovelace"
        assert take2.out_unit == USDM_UNIT


def test_drained_side_parses_with_zero_available() -> None:
    datum = vector_datum()
    take1 = _state(datum, {"lovelace": 10_000_000})
    take2 = take1.opposite()
    assert take2.available == Assets(root={USDM_UNIT: 0})
    assert take1.available == Assets(root={"lovelace": 10_000_000})
    assert take1.claimable_offer(CPUB) > 0
    token_drained = _state(_token_pair_datum(), {"lovelace": 3_000_000, USDM_UNIT: 5})
    assert token_drained.available == Assets(root={TEST_UNIT: 0})
    assert token_drained.opposite().available == Assets(root={USDM_UNIT: 5})


def test_expired_swap_is_inactive() -> None:
    expired = _ada_pair(expiration=CardanoSwapsSomeInt(60_000))
    assert expired.inactive is True
    assert expired.opposite().inactive is True
    assert _ada_pair(expiration=CardanoSwapsSomeInt(FAR_FUTURE)).inactive is False
    assert _ada_pair().inactive is False


def test_get_amount_out_at_price() -> None:
    state = _ada_pair(take_asset2=True)
    out, _ = state.get_amount_out(Assets(root={"lovelace": 1_900_000}))
    assert out == Assets(root={USDM_UNIT: 3})
    take1 = _ada_pair()
    out, _ = take1.get_amount_out(Assets(root={USDM_UNIT: 3}))
    assert out == Assets(root={"lovelace": 2_000_000})


def test_class_is_exported_and_shares_the_dex() -> None:
    import charli3_dendrite
    from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderState
    from charli3_dendrite.dexs.ob.cardanoswaps import _CardanoSwapsOrderState

    assert charli3_dendrite.CardanoSwapsTwoWayOrderState is CardanoSwapsTwoWayOrderState
    assert issubclass(CardanoSwapsTwoWayOrderState, _CardanoSwapsOrderState)
    assert CardanoSwapsTwoWayOrderState.dex() == CardanoSwapsOrderState.dex()
    assert CardanoSwapsTwoWayOrderState.order_datum_class() is (
        CardanoSwapsTwoWaySwapDatum
    )
    for name in ("swap_utxo", "build_close"):
        assert name not in CardanoSwapsTwoWayOrderState.__dict__


def test_pool_selector_discovers_by_the_two_way_beacon_policy() -> None:
    selector = CardanoSwapsTwoWayOrderState.pool_selector()
    assert selector.addresses == []
    assert selector.assets == [TWO_WAY_BEACON_POLICY_ID]
    assert CardanoSwapsTwoWayOrderState.dex_policy() == [TWO_WAY_BEACON_POLICY_ID]


# --- take ---------------------------------------------------------------------


def _take(
    state: CardanoSwapsTwoWayOrderState,
    offer_taken: int,
    tx_builder: TransactionBuilder,
    **kwargs,
):
    """Take ``offer_taken`` of the state's out asset, paying its required ask."""
    return state.swap_utxo(
        address_source=OWNER,
        in_assets=Assets(root={state.in_unit: state.required_ask(offer_taken)}),
        out_assets=Assets(root={state.out_unit: offer_taken}),
        tx_builder=tx_builder,
        owner_address=OWNER,
        **kwargs,
    )


def _assert_continues(state, cont_txo, cont_datum, tx_builder) -> None:
    """The continuation keeps the swap address, beacons and datum but prev_input."""
    datum = state.order_datum
    assert bytes(cont_txo.address.payment_part).hex() == TWO_WAY_SWAP_VALIDATOR_HASH
    assert cont_txo.address.staking_part == OWNER.staking_part
    assert set(datum.beacon_assets(1).root) <= _output_units(cont_txo)
    assert isinstance(cont_datum.prev_input, CardanoSwapsSomeOutRefV3)
    assert cont_datum.prev_input_ref() == (bytes.fromhex(TX_HASH), state.tx_index)
    assert cont_datum == datum.continuation_datum(
        bytes.fromhex(TX_HASH), state.tx_index
    )
    assert cont_txo.datum == cont_datum
    assert _mint_dict(tx_builder) == {}
    assert tx_builder.outputs == [cont_txo]


def test_take_asset1_partial_from_ada_pair(tx_builder) -> None:
    state = _ada_pair()
    assert state.required_ask(1_000_000) == 2  # ceil(1_000_000 * 3 / 2_000_000)
    cont_txo, cont_datum = _take(state, 1_000_000, tx_builder)
    assert cont_txo.amount.coin == 10_000_000 - 1_000_000
    assert _held(cont_txo, USDM_UNIT) == 30 + 2
    _assert_continues(state, cont_txo, cont_datum, tx_builder)
    assert [r.data.to_cbor_hex() for r in _redeemers(tx_builder)] == ["d87b80"]
    assert isinstance(_redeemers(tx_builder)[0].data, TakeAsset1)


def test_take_asset2_partial_from_ada_pair(tx_builder) -> None:
    state = _ada_pair(take_asset2=True)
    assert state.required_ask(3) == 1_900_000
    cont_txo, cont_datum = _take(state, 3, tx_builder)
    assert cont_txo.amount.coin == 10_000_000 + 1_900_000
    assert _held(cont_txo, USDM_UNIT) == 30 - 3
    _assert_continues(state, cont_txo, cont_datum, tx_builder)
    assert [r.data.to_cbor_hex() for r in _redeemers(tx_builder)] == ["d87c80"]
    assert isinstance(_redeemers(tx_builder)[0].data, TakeAsset2)


def test_take_token_pair_both_directions() -> None:
    tb1 = TransactionBuilder(_OfflineContext())
    take1 = _token_pair()
    assert take1.required_ask(100) == 150  # ceil(100 * 3 / 2) USDM
    cont1, datum1 = _take(take1, 100, tb1)
    assert (_held(cont1, TEST_UNIT), _held(cont1, USDM_UNIT)) == (900, 1_150)
    assert cont1.amount.coin == 4_000_000
    _assert_continues(take1, cont1, datum1, tb1)

    tb2 = TransactionBuilder(_OfflineContext())
    take2 = take1.opposite()
    assert take2.required_ask(70) == 50  # ceil(70 * 5 / 7) TEST
    cont2, datum2 = _take(take2, 70, tb2)
    assert (_held(cont2, TEST_UNIT), _held(cont2, USDM_UNIT)) == (1_050, 930)
    assert cont2.amount.coin == 4_000_000
    _assert_continues(take2, cont2, datum2, tb2)


@pytest.mark.parametrize(
    ("take_asset2", "taken", "expected"),
    [(False, 10, (90, 120)), (True, 30, (110, 70))],
    ids=["take-asset1", "take-asset2"],
)
def test_take_same_policy_token_pair_keeps_both_names(
    tx_builder,
    take_asset2,
    taken,
    expected,
) -> None:
    datum = _same_policy_datum()
    balance = {"lovelace": 3_000_000, SAME_A_UNIT: 100, SAME_B_UNIT: 100}
    state = _state(datum, balance, take_asset2=take_asset2)
    assert (state.in_unit, state.out_unit) == (
        (SAME_A_UNIT, SAME_B_UNIT) if take_asset2 else (SAME_B_UNIT, SAME_A_UNIT)
    )
    cont_txo, cont_datum = _take(state, taken, tx_builder)
    assert (_held(cont_txo, SAME_A_UNIT), _held(cont_txo, SAME_B_UNIT)) == expected
    assert (cont_datum.asset1_name, cont_datum.asset2_name) == (NAME_A, NAME_B)
    _assert_continues(state, cont_txo, cont_datum, tx_builder)


def test_take_rejects_ada_over_claim() -> None:
    state = _state(vector_datum(), {"lovelace": 5_000_000, USDM_UNIT: 30})
    claimable = state.claimable_offer(CPUB)
    assert 0 < claimable < 5_000_000
    with pytest.raises(ValueError, match="min-UTxO"):
        _take(state, 5_000_000, TransactionBuilder(_OfflineContext()))

    tb = TransactionBuilder(_OfflineContext())
    cont_txo, _ = _take(state, claimable, tb)
    assert cont_txo.amount.coin == 5_000_000 - claimable
    assert min_lovelace(tb.context, output=cont_txo) <= cont_txo.amount.coin


def test_take_of_an_expiring_swap_sets_its_deadline() -> None:
    state = _ada_pair(expiration=CardanoSwapsSomeInt(EXPIRATION))
    deadline = posix_ms_to_slot(EXPIRATION)
    tb = TransactionBuilder(_ContextAt(deadline - 600))
    _take(state, 1_000_000, tb)
    assert tb.ttl == deadline


def test_take_references_the_swap_script_when_supplied(tx_builder) -> None:
    swap_ref = _ref_utxo(CardanoSwapsTwoWayOrderState._swap_script())
    _take(_ada_pair(), 1_000_000, tx_builder, swap_ref_utxo=swap_ref)
    assert swap_ref in tx_builder.reference_inputs

    from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsOrderState

    one_way_ref = _ref_utxo(CardanoSwapsOrderState._swap_script())
    with pytest.raises(ValueError, match="script hash"):
        _take(
            _ada_pair(),
            1_000_000,
            TransactionBuilder(_OfflineContext()),
            swap_ref_utxo=one_way_ref,
        )


def test_take_writes_no_witness_datum(tx_builder) -> None:
    _take(_ada_pair(take_asset2=True), 3, tx_builder)
    assert tx_builder.datums == {}


# --- close --------------------------------------------------------------------


def test_close_burns_three_beacons(tx_builder) -> None:
    state = _ada_pair()
    spent = state.build_close(tx_builder=tx_builder, owner_address=OWNER)
    assert spent == state.order_datum
    mint = _mint_dict(tx_builder)
    assert mint == {unit: -1 for unit in state.order_datum.beacon_assets(1).root}
    assert len(mint) == 3
    assert all(unit.startswith(TWO_WAY_BEACON_POLICY_ID) for unit in mint)
    assert [type(r.data) for r in _redeemers(tx_builder)] == [SpendWithMint]
    (mint_redeemer,) = list(tx_builder._minting_script_to_redeemers)
    assert isinstance(mint_redeemer[1].data, CreateOrCloseSwaps)
    assert tx_builder.outputs == []


def test_close_references_both_scripts_when_supplied(tx_builder) -> None:
    swap_ref = _ref_utxo(CardanoSwapsTwoWayOrderState._swap_script(), index=0)
    beacon_ref = _ref_utxo(CardanoSwapsTwoWayOrderState._beacon_script(), index=1)
    _ada_pair(take_asset2=True).build_close(
        tx_builder=tx_builder,
        owner_address=OWNER,
        swap_ref_utxo=swap_ref,
        beacon_ref_utxo=beacon_ref,
    )
    assert swap_ref in tx_builder.reference_inputs
    assert beacon_ref in tx_builder.reference_inputs
    assert len(_mint_dict(tx_builder)) == 3
