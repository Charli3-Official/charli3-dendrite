"""Cardano-Swaps two-way swap datum: pricing, continuation and carrier sizing."""

from __future__ import annotations

import pytest
from pycardano import TransactionOutput

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsRational
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRefV3
from charli3_dendrite.dexs.ob.cardanoswaps import _self_funded_min_lovelace
from charli3_dendrite.dexs.ob.cardanoswaps import _SIZING_ADDRESS
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import TWO_WAY_BEACON_POLICY_ID
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import CardanoSwapsTwoWaySwapDatum
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import two_way_asset_beacon_name
from charli3_dendrite.dexs.ob.cardanoswaps_two_way import two_way_pair_beacon_name
from charli3_dendrite.utility import asset_to_value
from tests.test_cardanoswaps import _OfflineContext
from tests.test_cardanoswaps_two_way_contracts import TEST_NAME
from tests.test_cardanoswaps_two_way_contracts import TEST_POLICY
from tests.test_cardanoswaps_two_way_contracts import USDM_ID
from tests.test_cardanoswaps_two_way_contracts import USDM_NAME
from tests.test_cardanoswaps_two_way_contracts import USDM_UNIT
from tests.test_cardanoswaps_two_way_contracts import vector_datum

CPB = _OfflineContext().protocol_param.coins_per_utxo_byte
TEST_UNIT = TEST_POLICY.hex() + TEST_NAME.hex()


def _token_pair_datum() -> CardanoSwapsTwoWaySwapDatum:
    """A TEST/USDM two-way swap (TEST sorts first by policy id)."""
    return CardanoSwapsTwoWaySwapDatum(
        beacon_id=bytes.fromhex(TWO_WAY_BEACON_POLICY_ID),
        pair_beacon=two_way_pair_beacon_name(
            TEST_POLICY, TEST_NAME, USDM_ID, USDM_NAME
        ),
        asset1_id=TEST_POLICY,
        asset1_name=TEST_NAME,
        asset1_beacon=two_way_asset_beacon_name(TEST_POLICY, TEST_NAME),
        asset2_id=USDM_ID,
        asset2_name=USDM_NAME,
        asset2_beacon=two_way_asset_beacon_name(USDM_ID, USDM_NAME),
        asset1_price=CardanoSwapsRational(3, 2),
        asset2_price=CardanoSwapsRational(5, 7),
        prev_input=PlutusNone(),
        expiration=PlutusNone(),
    )


def test_units_and_order_interface() -> None:
    datum = vector_datum()
    assert datum.asset1_unit() == "lovelace"
    assert datum.asset2_unit() == USDM_UNIT
    assert datum.offer_unit(take_asset2=False) == "lovelace"
    assert datum.ask_unit(take_asset2=False) == USDM_UNIT
    assert datum.offer_unit(take_asset2=True) == USDM_UNIT
    assert datum.ask_unit(take_asset2=True) == "lovelace"
    both = Assets(root={"lovelace": 0, USDM_UNIT: 0})
    assert datum.pool_pair() == both
    assert datum.requested_amount() == both
    assert datum.address_source() is None
    assert datum.order_type() == OrderType.swap
    assert datum.prev_input_ref() is None


def test_price_and_required_ask_per_direction() -> None:
    datum = vector_datum()
    assert datum.price(take_asset2=False) == (3, 2_000_000)
    assert datum.required_ask(2_000_000, take_asset2=False) == 3
    assert datum.required_ask(1, take_asset2=False) == 1
    assert datum.price(take_asset2=True) == (1_900_000, 3)
    assert datum.required_ask(3, take_asset2=True) == 1_900_000


@pytest.mark.parametrize("take_asset2", [False, True])
@pytest.mark.parametrize("bad", [(0, 1), (1, 0), (-1, 1), (1, -1)])
def test_non_positive_price_raises_invalid_pool_error(
    take_asset2: bool,
    bad: tuple[int, int],
) -> None:
    datum = (
        vector_datum(asset2_price=bad)
        if take_asset2
        else vector_datum(asset1_price=bad)
    )
    with pytest.raises(InvalidPoolError):
        datum.price(take_asset2=take_asset2)
    with pytest.raises(InvalidPoolError):
        datum.required_ask(1, take_asset2=take_asset2)


def test_beacon_assets_and_continuation_datum() -> None:
    datum = vector_datum()
    policy = datum.beacon_id.hex()
    assert datum.beacon_assets(2) == Assets(
        root={
            policy + datum.pair_beacon.hex(): 2,
            policy + datum.asset1_beacon.hex(): 2,
            policy + datum.asset2_beacon.hex(): 2,
        },
    )
    cont = datum.continuation_datum(bytes.fromhex("ab" * 32), 5)
    assert cont.prev_input == CardanoSwapsSomeOutRefV3.from_ref(
        bytes.fromhex("ab" * 32),
        5,
    )
    assert cont.prev_input_ref() == (bytes.fromhex("ab" * 32), 5)
    assert datum.prev_input_ref() is None


def test_continuation_min_lovelace_adds_the_prev_input_bytes() -> None:
    """The continuation outgrows the resting output by the Some(prev_input) bytes."""
    datum = vector_datum()
    held = Assets(root={USDM_UNIT: 20_000_000})
    resting = TransactionOutput(
        address=_SIZING_ADDRESS,
        amount=asset_to_value(held + datum.beacon_assets(1)),
        datum=datum,
    )
    resting_min = _self_funded_min_lovelace(resting, CPB)
    growth = len(datum.continuation_datum(bytes(32), 0).to_cbor()) - len(
        datum.to_cbor(),
    )
    cont_min = datum.continuation_min_lovelace(held, CPB)
    assert cont_min > resting_min
    assert cont_min - resting_min == growth * CPB
    # Lovelace in ``held`` is ignored: the balance is sized as the minimum.
    assert (
        datum.continuation_min_lovelace(held + Assets(root={"lovelace": 10**12}), CPB)
        == cont_min
    )


def test_claimable_token_side_is_the_whole_balance() -> None:
    assert vector_datum().claimable_offer(50, CPB, take_asset2=True) == 50


def test_claimable_ada_counts_the_held_token() -> None:
    """The token held in the swap widens the continuation, shrinking claimable ADA."""
    datum = vector_datum()
    gross = 100_000_000
    light = datum.claimable_offer(gross, CPB, take_asset2=False, held_ask=0)
    heavy = datum.claimable_offer(gross, CPB, take_asset2=False, held_ask=10**15)
    assert 0 < heavy < light < gross


def test_claimable_ada_is_floored_at_zero() -> None:
    assert vector_datum().claimable_offer(1, CPB, take_asset2=False) == 0


@pytest.mark.parametrize("asset2_deposit", [0, 5_000_000])
@pytest.mark.parametrize("ada_deposit", [1, 2_000_000, 10**12])
def test_carrier_makes_the_whole_ada_deposit_claimable(
    ada_deposit: int,
    asset2_deposit: int,
) -> None:
    datum = vector_datum()
    carrier = datum.carrier_lovelace(ada_deposit, asset2_deposit, CPB)
    assert carrier > 0
    claimable = datum.claimable_offer(
        ada_deposit + carrier,
        CPB,
        take_asset2=False,
        held_ask=asset2_deposit,
    )
    assert claimable == ada_deposit


def test_token_pair_carrier_holds_both_full_balances() -> None:
    """A token pair's carrier is the whole lovelace, sized for both sides full."""
    datum = _token_pair_datum()
    asset1, asset2 = 1_000_000, 3_000_000
    carrier = datum.carrier_lovelace(asset1, asset2, CPB)
    full = Assets(
        root={
            TEST_UNIT: asset1 + datum.required_ask(asset2, take_asset2=True),
            USDM_UNIT: asset2 + datum.required_ask(asset1, take_asset2=False),
        },
    )
    assert carrier == datum.continuation_min_lovelace(full, CPB)
    deposits = Assets(root={TEST_UNIT: asset1, USDM_UNIT: asset2})
    assert carrier >= datum.continuation_min_lovelace(deposits, CPB)
    assert datum.claimable_offer(asset1, CPB, take_asset2=False) == asset1
    assert datum.claimable_offer(asset2, CPB, take_asset2=True) == asset2
