"""Cardano-Swaps two-way order state: parse, orient, take and close.

A two-way swap UTxO quotes both directions of its pair. Each
``CardanoSwapsTwoWayOrderState`` is one direction's view of it: ``take_asset2``
selects the direction, and the assets are oriented for the taker (``in`` = the
asset paid, ``out`` = the asset taken).
"""

from __future__ import annotations

import dataclasses

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import VerificationKeyHash
from pycardano import Withdrawals
from pycardano.utils import min_lovelace

from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsRational
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeInt
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRefV3
from charli3_dendrite.dexs.ob.cardanoswaps import CreateOrCloseSwaps
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithMint
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithStake
from charli3_dendrite.dexs.ob.cardanoswaps import UpdateSwaps
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import KEEP_EXPIRATION
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_CONTRACT
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
    assert take2.claimable_offer(CPUB) == 0
    token_drained = _state(_token_pair_datum(), {"lovelace": 3_000_000, USDM_UNIT: 5})
    assert token_drained.available == Assets(root={TEST_UNIT: 0})
    assert token_drained.claimable_offer(CPUB) == 0
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


def test_close_adds_the_owner_stake_signer(tx_builder) -> None:
    _ada_pair().build_close(tx_builder=tx_builder, owner_address=OWNER)
    assert tx_builder.required_signers == [OWNER.staking_part]


def _at_owner(state: CardanoSwapsTwoWayOrderState) -> CardanoSwapsTwoWayOrderState:
    """``state`` parsed with its address: :data:`OWNER`'s swap address."""
    state.address = str(CardanoSwapsTwoWayOrderState.beacon_address(OWNER))
    return state


OTHER_OWNER = Address(
    payment_part=VerificationKeyHash(bytes.fromhex("11" * 28)),
    staking_part=VerificationKeyHash(bytes.fromhex("44" * 28)),
    network=Network.MAINNET,
)


def test_close_rejects_an_owner_other_than_the_swap_address(tx_builder) -> None:
    state = _at_owner(_ada_pair())
    with pytest.raises(ValueError, match="staking credential"):
        state.build_close(tx_builder=tx_builder, owner_address=OTHER_OWNER)
    assert tx_builder.inputs == [] and tx_builder.mint is None
    state.build_close(tx_builder=tx_builder, owner_address=OWNER)
    assert len(tx_builder.inputs) == 1


# --- create -------------------------------------------------------------------

ADA_DEPOSIT = 10_000_000
USDM_DEPOSIT = 30

SCRIPT_STAKED_OWNER = Address(
    payment_part=VerificationKeyHash(bytes.fromhex("11" * 28)),
    staking_part=ScriptHash(bytes.fromhex("33" * 28)),
    network=Network.MAINNET,
)


def _create(
    tx_builder: TransactionBuilder,
    asset1: Assets | None = None,
    asset2: Assets | None = None,
    *,
    asset1_price: tuple[int, int] = (3, 2_000_000),
    asset2_price: tuple[int, int] = (1_900_000, 3),
    owner: Address = OWNER,
    **kwargs,
):
    """Create the ADA/USDM vector swap (10 ADA and 30 USDM) unless overridden."""
    return CardanoSwapsTwoWayOrderState.build_create(
        owner_address=owner,
        asset1=Assets(root={"lovelace": ADA_DEPOSIT}) if asset1 is None else asset1,
        asset2=Assets(root={USDM_UNIT: USDM_DEPOSIT}) if asset2 is None else asset2,
        asset1_price=asset1_price,
        asset2_price=asset2_price,
        tx_builder=tx_builder,
        **kwargs,
    )


def test_create_mints_three_beacons_and_writes_the_datum(tx_builder) -> None:
    txo, datum = _create(tx_builder)
    assert datum == vector_datum()
    assert txo.datum == datum
    assert tx_builder.outputs == [txo]
    assert _mint_dict(tx_builder) == {unit: 1 for unit in datum.beacon_assets(1).root}
    assert _output_units(txo) == {*datum.beacon_assets(1).root, USDM_UNIT}
    assert _held(txo, USDM_UNIT) == USDM_DEPOSIT
    assert txo.address.payment_part == ScriptHash(
        bytes.fromhex(TWO_WAY_SWAP_VALIDATOR_HASH),
    )
    assert txo.address.staking_part == OWNER.staking_part
    assert txo.script is None
    assert _redeemers(tx_builder) == []
    (mint_redeemer,) = list(tx_builder._minting_script_to_redeemers)
    assert isinstance(mint_redeemer[1].data, CreateOrCloseSwaps)


def test_create_ada_pair_funds_a_drawable_carrier(tx_builder) -> None:
    txo, datum = _create(tx_builder)
    carrier = datum.carrier_lovelace(
        ADA_DEPOSIT,
        USDM_DEPOSIT,
        CPUB,
        prev_output_index=0,
        address=txo.address,
    )
    assert txo.amount.coin == ADA_DEPOSIT + carrier
    take_asset1 = _state(datum, {"lovelace": txo.amount.coin, USDM_UNIT: USDM_DEPOSIT})
    assert take_asset1.claimable_offer(CPUB) == ADA_DEPOSIT
    assert take_asset1.opposite().claimable_offer(CPUB) == USDM_DEPOSIT


def test_create_token_pair_carries_its_min_utxo(tx_builder) -> None:
    expected = _token_pair_datum()
    txo, datum = _create(
        tx_builder,
        Assets(root={TEST_UNIT: 1_000}),
        Assets(root={USDM_UNIT: 1_000}),
        asset1_price=(3, 2),
        asset2_price=(5, 7),
    )
    assert datum == expected
    carrier = datum.carrier_lovelace(
        1_000,
        1_000,
        CPUB,
        prev_output_index=0,
        address=txo.address,
    )
    assert txo.amount.coin == max(carrier, min_lovelace(tx_builder.context, output=txo))
    assert txo.amount.coin >= min_lovelace(tx_builder.context, output=txo)
    assert _held(txo, TEST_UNIT) == 1_000
    assert _held(txo, USDM_UNIT) == 1_000


def test_create_with_one_side_empty_holds_only_the_other(tx_builder) -> None:
    txo, datum = _create(tx_builder, asset2=Assets(root={USDM_UNIT: 0}))
    assert _output_units(txo) == set(datum.beacon_assets(1).root)
    carrier = datum.carrier_lovelace(
        ADA_DEPOSIT,
        0,
        CPUB,
        prev_output_index=0,
        address=txo.address,
    )
    assert txo.amount.coin == ADA_DEPOSIT + carrier


def test_create_carrier_is_sized_at_the_created_output_index(tx_builder) -> None:
    for _ in range(24):
        tx_builder.add_output(TransactionOutput(OWNER, 2_000_000))
    txo, datum = _create(tx_builder)
    assert tx_builder.outputs[24] is txo
    carrier = datum.carrier_lovelace(
        ADA_DEPOSIT,
        USDM_DEPOSIT,
        CPUB,
        prev_output_index=24,
        address=txo.address,
    )
    assert txo.amount.coin == ADA_DEPOSIT + carrier


def test_create_adds_the_owner_stake_signer_once(tx_builder) -> None:
    tx_builder.required_signers = [OWNER.staking_part]
    _create(tx_builder)
    assert tx_builder.required_signers == [OWNER.staking_part]

    fresh = TransactionBuilder(_OfflineContext())
    _create(fresh)
    assert fresh.required_signers == [OWNER.staking_part]


def test_create_with_expiration_sets_the_deadline() -> None:
    deadline = posix_ms_to_slot(EXPIRATION)
    tb = TransactionBuilder(_ContextAt(deadline - 600))
    _, datum = _create(tb, expiration=EXPIRATION)
    assert datum == vector_datum(expiration=CardanoSwapsSomeInt(EXPIRATION))
    assert tb.ttl == deadline

    far = TransactionBuilder(_ContextAt(1_000))
    _create(far, expiration=EXPIRATION)
    assert far.ttl == 1_000 + 3_600

    earlier = TransactionBuilder(_ContextAt(deadline - 600))
    earlier.ttl = deadline - 100
    _create(earlier, expiration=EXPIRATION)
    assert earlier.ttl == deadline - 100


def test_create_without_expiration_leaves_the_validity_open(tx_builder) -> None:
    _create(tx_builder)
    assert tx_builder.ttl is None


def test_create_references_the_beacon_script_when_supplied(tx_builder) -> None:
    beacon_ref = _ref_utxo(CardanoSwapsTwoWayOrderState._beacon_script())
    _create(tx_builder, beacon_ref_utxo=beacon_ref)
    assert beacon_ref in tx_builder.reference_inputs
    assert tx_builder.scripts == []
    assert all(qty == 1 for qty in _mint_dict(tx_builder).values())

    inline = TransactionBuilder(_OfflineContext())
    _create(inline)
    assert inline.reference_inputs == set()
    assert inline.scripts == [CardanoSwapsTwoWayOrderState._beacon_script()]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {
                "asset1": Assets(root={USDM_UNIT: 30}),
                "asset2": Assets(root={"lovelace": 10_000_000}),
            },
            id="unsorted",
        ),
        pytest.param(
            {
                "asset1": Assets(root={USDM_UNIT: 30}),
                "asset2": Assets(root={USDM_UNIT: 30}),
            },
            id="equal",
        ),
        pytest.param({"asset1_price": (0, 1)}, id="zero-asset1-numerator"),
        pytest.param({"asset1_price": (3, 0)}, id="zero-asset1-denominator"),
        pytest.param({"asset2_price": (-1, 3)}, id="negative-asset2-numerator"),
        pytest.param({"asset2_price": (1, -3)}, id="negative-asset2-denominator"),
        pytest.param(
            {
                "asset1": Assets(root={"lovelace": 0}),
                "asset2": Assets(root={USDM_UNIT: 0}),
            },
            id="both-deposits-zero",
        ),
        pytest.param({"asset2": Assets(root={USDM_UNIT: -1})}, id="negative-deposit"),
        pytest.param(
            {"asset1": Assets(root={"lovelace": 10_000_000, TEST_UNIT: 5})},
            id="two-unit-bag",
        ),
        pytest.param({"expiration": EXPIRATION + 1}, id="expiration-not-a-minute"),
        pytest.param({"owner": SCRIPT_STAKED_OWNER}, id="script-staked-owner"),
    ],
)
def test_create_rejects_bad_input(tx_builder, overrides) -> None:
    with pytest.raises(ValueError):
        _create(tx_builder, **overrides)
    assert tx_builder.outputs == []
    assert tx_builder.mint is None
    assert tx_builder.required_signers is None
    assert tx_builder.ttl is None
    assert list(tx_builder._minting_script_to_redeemers) == []


# --- update -------------------------------------------------------------------

PREV_INPUT = CardanoSwapsSomeOutRefV3.from_ref(bytes.fromhex("ab" * 32), 5)


def _update(state: CardanoSwapsTwoWayOrderState, tx_builder, **kwargs):
    """Update ``state`` in place for :data:`OWNER`."""
    return state.build_update(tx_builder, OWNER, **kwargs)


def _withdrawals(tx_builder: TransactionBuilder) -> dict[bytes, int]:
    return dict(tx_builder.withdrawals or {})


def _withdrawal_redeemers(tx_builder: TransactionBuilder) -> list:
    return [r for _, r in tx_builder._withdrawal_script_to_redeemers]


def test_update_reprices_in_place(tx_builder) -> None:
    state = _ada_pair(prev_input=PREV_INPUT)
    txo, datum = _update(state, tx_builder, asset1_price=(4, 2_000_000))

    old = state.order_datum
    assert datum == dataclasses.replace(
        old,
        asset1_price=CardanoSwapsRational(4, 2_000_000),
        prev_input=PlutusNone(),
    )
    assert datum.asset2_price == old.asset2_price
    assert txo.datum == datum
    assert tx_builder.outputs == [txo]

    spent = state._input_utxo(OWNER)
    assert tx_builder.inputs == [spent]
    assert [type(r.data) for r in _redeemers(tx_builder)] == [SpendWithStake]
    assert SpendWithStake.CONSTR_ID == 1
    assert tx_builder.mint is None
    assert list(tx_builder._minting_script_to_redeemers) == []

    reward = TWO_WAY_CONTRACT.beacon_reward_address(OWNER.network)
    assert _withdrawals(tx_builder) == {bytes(reward): 0}
    assert [type(r.data) for r in _withdrawal_redeemers(tx_builder)] == [UpdateSwaps]

    assert txo.address == spent.output.address
    assert txo.amount == spent.output.amount
    assert tx_builder.ttl is None


def test_update_refunds_the_ada_side(tx_builder) -> None:
    txo, datum = _update(_ada_pair(), tx_builder, asset1_quantity=5_000_000)
    carrier = datum.carrier_lovelace(
        5_000_000,
        30,
        CPUB,
        prev_output_index=0,
        address=txo.address,
    )
    assert txo.amount.coin == 5_000_000 + carrier
    assert _held(txo, USDM_UNIT) == 30
    assert _output_units(txo) == {*datum.beacon_assets(1).root, USDM_UNIT}


def test_update_refunds_the_token_side(tx_builder) -> None:
    txo, datum = _update(_ada_pair(), tx_builder, asset2_quantity=0)
    assert _output_units(txo) == set(datum.beacon_assets(1).root)
    assert txo.amount.coin == 10_000_000


def test_update_sizes_a_token_pair_lovelace(tx_builder) -> None:
    state = _token_pair()
    txo, datum = _update(state, tx_builder, asset2_quantity=50_000)
    carrier = datum.carrier_lovelace(
        1_000,
        50_000,
        CPUB,
        prev_output_index=0,
        address=txo.address,
    )
    assert txo.amount.coin == max(
        4_000_000,
        carrier,
        min_lovelace(tx_builder.context, output=txo),
    )
    assert _held(txo, TEST_UNIT) == 1_000
    assert _held(txo, USDM_UNIT) == 50_000

    kept = TransactionBuilder(_OfflineContext())
    txo, _ = _update(_token_pair(), kept, asset1_price=(1, 1))
    assert txo.amount.coin == 4_000_000


@pytest.mark.parametrize(
    ("expiration", "expected"),
    [
        pytest.param(KEEP_EXPIRATION, EXPIRATION, id="keep"),
        pytest.param(None, None, id="clear"),
        pytest.param(EXPIRATION + 60_000, EXPIRATION + 60_000, id="replace"),
    ],
)
def test_update_keeps_or_clears_the_expiration(expiration, expected) -> None:
    state = _ada_pair(expiration=CardanoSwapsSomeInt(EXPIRATION))
    tb = TransactionBuilder(_ContextAt(posix_ms_to_slot(EXPIRATION) - 600))
    _, datum = _update(state, tb, expiration=expiration)
    if expected is None:
        assert datum.expiration == PlutusNone()
        assert tb.ttl is None
    else:
        assert datum.expiration == CardanoSwapsSomeInt(expected)
        assert tb.ttl == posix_ms_to_slot(expected)


def _assert_untouched(tx_builder: TransactionBuilder) -> None:
    assert tx_builder.inputs == []
    assert tx_builder.outputs == []
    assert tx_builder.withdrawals is None
    assert tx_builder._withdrawal_script_to_redeemers == []
    assert tx_builder.required_signers is None
    assert tx_builder.ttl is None


def test_update_rejects_a_held_balance_below_the_new_min_utxo(tx_builder) -> None:
    state = _state(vector_datum(), {"lovelace": 1_000_000, USDM_UNIT: 30})
    with pytest.raises(ValueError, match="min-UTxO"):
        _update(state, tx_builder, asset2_price=(2_000_000, 3))
    _assert_untouched(tx_builder)

    txo, _ = _update(state, tx_builder, asset1_quantity=1_000_000)
    assert txo.amount.coin > 1_000_000


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"asset1_price": (0, 1)}, id="zero-asset1-numerator"),
        pytest.param({"asset2_price": (1, -3)}, id="negative-asset2-denominator"),
        pytest.param(
            {"asset1_quantity": 0, "asset2_quantity": 0},
            id="both-balances-zero",
        ),
        pytest.param({"asset2_quantity": -1}, id="negative-balance"),
        pytest.param({"expiration": EXPIRATION + 1}, id="expiration-not-a-minute"),
        pytest.param({"owner": SCRIPT_STAKED_OWNER}, id="script-staked-owner"),
    ],
)
def test_update_rejects_bad_input(tx_builder, overrides) -> None:
    owner = overrides.pop("owner", OWNER)
    with pytest.raises(ValueError):
        _ada_pair().build_update(tx_builder, owner, **overrides)
    _assert_untouched(tx_builder)


def test_update_adds_the_owner_stake_signer(tx_builder) -> None:
    _update(_ada_pair(), tx_builder, asset1_price=(4, 2_000_000))
    assert tx_builder.required_signers == [OWNER.staking_part]


def test_update_references_both_scripts_when_supplied(tx_builder) -> None:
    swap_ref = _ref_utxo(CardanoSwapsTwoWayOrderState._swap_script(), index=0)
    beacon_ref = _ref_utxo(CardanoSwapsTwoWayOrderState._beacon_script(), index=1)
    _update(
        _ada_pair(),
        tx_builder,
        asset2_quantity=10,
        swap_ref_utxo=swap_ref,
        beacon_ref_utxo=beacon_ref,
    )
    assert swap_ref in tx_builder.reference_inputs
    assert beacon_ref in tx_builder.reference_inputs
    assert tx_builder.scripts == []

    inline = TransactionBuilder(_OfflineContext())
    _update(_ada_pair(), inline, asset2_quantity=10)
    assert inline.reference_inputs == set()
    assert len(inline.scripts) == 2
    assert set(inline.scripts) == {
        CardanoSwapsTwoWayOrderState._swap_script(),
        CardanoSwapsTwoWayOrderState._beacon_script(),
    }


def test_update_two_swaps_in_one_transaction_withdraws_once(tx_builder) -> None:
    first = _ada_pair()
    second = _state(
        vector_datum(),
        {"lovelace": 10_000_000, USDM_UNIT: 30},
        tx_index=1,
    )
    _update(first, tx_builder, asset2_quantity=10)
    _update(second, tx_builder, asset2_quantity=20)
    reward = TWO_WAY_CONTRACT.beacon_reward_address(OWNER.network)
    assert _withdrawals(tx_builder) == {bytes(reward): 0}
    assert len(_withdrawal_redeemers(tx_builder)) == 1
    assert len(tx_builder.inputs) == 2
    assert len(tx_builder.outputs) == 2


@pytest.mark.parametrize(
    ("expiration", "tip"),
    [
        pytest.param(KEEP_EXPIRATION, posix_ms_to_slot(EXPIRATION), id="kept-at-tip"),
        pytest.param(
            KEEP_EXPIRATION, posix_ms_to_slot(EXPIRATION) + 60, id="kept-past"
        ),
        pytest.param(EXPIRATION, posix_ms_to_slot(EXPIRATION) + 60, id="new-past"),
    ],
)
def test_update_rejects_an_expiration_already_reached(expiration, tip) -> None:
    state = _ada_pair(expiration=CardanoSwapsSomeInt(EXPIRATION))
    tb = TransactionBuilder(_ContextAt(tip))
    with pytest.raises(ValueError, match="expiration"):
        _update(state, tb, expiration=expiration)
    _assert_untouched(tb)

    _, datum = _update(state, tb, expiration=None)
    assert datum.expiration == PlutusNone()


def test_update_rejects_a_carrier_only_ada_side_with_no_token(tx_builder) -> None:
    datum = vector_datum()
    carrier = datum.continuation_min_lovelace(Assets(root={}), CPUB)
    state = _state(datum, {"lovelace": carrier, USDM_UNIT: 30})
    with pytest.raises(ValueError, match="needs a balance"):
        _update(state, tx_builder, asset2_quantity=0)
    _assert_untouched(tx_builder)

    funded = _state(datum, {"lovelace": carrier + 1, USDM_UNIT: 30})
    txo, _ = _update(funded, tx_builder, asset2_quantity=0)
    assert _output_units(txo) == set(datum.beacon_assets(1).root)


def test_update_rejects_an_owner_other_than_the_swap_address(tx_builder) -> None:
    state = _at_owner(_ada_pair())
    with pytest.raises(ValueError, match="staking credential"):
        state.build_update(tx_builder, OTHER_OWNER, asset1_price=(4, 2_000_000))
    _assert_untouched(tx_builder)
    _update(state, tx_builder, asset1_price=(4, 2_000_000))
    assert len(tx_builder.inputs) == 1


def test_update_completes_a_beacon_withdrawal_set_by_the_caller() -> None:
    reward = bytes(TWO_WAY_CONTRACT.beacon_reward_address(OWNER.network))
    preset = TransactionBuilder(_OfflineContext())
    preset.withdrawals = Withdrawals({reward: 0})
    _update(_ada_pair(), preset, asset2_quantity=10)
    assert _withdrawals(preset) == {reward: 0}
    assert [type(r.data) for r in _withdrawal_redeemers(preset)] == [UpdateSwaps]

    scripted = TransactionBuilder(_OfflineContext())
    scripted.add_withdrawal_script(
        CardanoSwapsTwoWayOrderState._beacon_script(),
        Redeemer(UpdateSwaps()),
    )
    _update(_ada_pair(), scripted, asset2_quantity=10)
    assert _withdrawals(scripted) == {reward: 0}
    assert len(_withdrawal_redeemers(scripted)) == 1
