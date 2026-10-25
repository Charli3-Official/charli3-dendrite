"""Supply and redeem: byte-exact replays of captured transactions and live builds.

The replays rebuild each captured supply and redeem from its snapshot and compare
every protocol output, redeemer, mint, withdrawal, signer, reference input and the
validity interval. The live builds resolve the same transactions through an offline
backend seeded from the capture.
"""

from __future__ import annotations

import dataclasses
import random
from typing import Any

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.datums import encode_action_datum
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions.errors import CapExceededError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MarketAwaitingBatchError
from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import _build_supply
from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import build_supply
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_burn_for
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_payout
from charli3_dendrite.lending.liqwid.transactions.terms import supply_mint
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.errors import ValidityWindowError
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.utility import posix_ms_to_slot
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.replay import Built
from tests.lending.liqwid.transactions.replay import assert_replays
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import captured_mints
from tests.lending.liqwid.transactions.replay import captured_protocol_outputs
from tests.lending.liqwid.transactions.replay import captured_redeemers
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import protocol_outputs
from tests.lending.liqwid.transactions.replay import redeemers
from tests.lending.liqwid.transactions.replay import required_signers
from tests.lending.liqwid.transactions.replay import validity

SUPPLIES = [
    "supply_ada",
    "supply_djed",
    "family_isolated_supply",
    "family_collateral_only_supply",
    "family_staked_supply",
]
REDEEMS = [
    "redeem_ada",
    "redeem_djed_floor",
    "family_isolated_redeem",
    "family_lp_redeem",
]
# Redeems built by Liqwid's app, which pays one base unit below the rate.
APP_REDEEMS = ["redeem_ada", "family_isolated_redeem", "family_lp_redeem"]
# Captures that run the qToken policy from a script in the witness set rather than
# from its reference UTxO, and a capture that references that UTxO.
INLINE_QTOKEN_SCRIPT = {"redeem_djed_floor": "supply_djed"}
CREDENTIALS = set(default_deployment().payment_credentials)
ADA_MIN_VALUE = 81_000_000
QADA = "a04ce7a52545e5e33c2867e148898d9e667a69602285f6a1298f9d68"


def _slot(fix: dict[str, Any]) -> int:
    return posix_ms_to_slot(fix["block_time"] * 1000)


def _supply(label: str) -> tuple[dict[str, Any], SupplySnapshot]:
    fix = fixture(label)
    return fix, SupplySnapshot.from_capture(fix)


def _redeem(label: str) -> tuple[dict[str, Any], RedeemSnapshot]:
    """A captured redeem and its snapshot.

    The builders run every script from its reference UTxO. A capture that carried
    the qToken policy in its witness set gets that policy's reference UTxO added to
    its reference inputs, so the replay expects exactly the captured references and
    that one.
    """
    fix = fixture(label)
    source = INLINE_QTOKEN_SCRIPT.get(label)
    if source is not None:
        fix["ref_inputs"].append(_qtoken_reference(fixture(source)))
    return fix, RedeemSnapshot.from_capture(fix)


def _qtoken_reference(fix: dict[str, Any]) -> dict[str, Any]:
    """The reference UTxO of the qToken policy ``fix`` mints or burns."""
    deployment = default_deployment()
    ((policy, _, _),) = fix["mints"]
    tx_hash, index = deployment.reference_utxo(policy).out_ref.split("#")
    return next(u for u in fix["ref_inputs"] if u["out_ref"] == [tx_hash, int(index)])


def test_the_inline_script_capture_lacks_only_the_qtoken_reference():
    captured = fixture("redeem_djed_floor")
    fix, snapshot = _redeem("redeem_djed_floor")
    added = [u for u in fix["ref_inputs"] if u not in captured["ref_inputs"]]
    assert [tuple(u["out_ref"]) for u in added] == [
        snapshot.script_refs.qtoken_policy.out_ref,
    ]


def _action_output_datum(built: Built, snapshot: SupplySnapshot | RedeemSnapshot):
    credential = snapshot.records.params_datum.action_validator
    (output,) = protocol_outputs(built, {credential})
    return output.datum


# --- replays -----------------------------------------------------------------


@pytest.mark.parametrize("label", SUPPLIES)
def test_a_captured_supply_replays(label):
    fix, snapshot = _supply(label)
    built = build(build_supply, snapshot, slot=_slot(fix))
    assert_replays(built, fix, credentials=CREDENTIALS)


@pytest.mark.parametrize("label", REDEEMS)
def test_a_captured_redeem_replays(label):
    fix, snapshot = _redeem(label)
    built = build(build_redeem, snapshot, slot=_slot(fix))
    assert_replays(built, fix, credentials=CREDENTIALS)


@pytest.mark.parametrize("label", SUPPLIES)
def test_a_captured_supply_mints_at_the_full_rate(label):
    fix, snapshot = _supply(label)
    rate = snapshot.records.state_datum.qtoken_rate
    assert snapshot.qtokens == supply_mint(snapshot.amount, rate)
    assert captured_mints(fix) == {(snapshot.market.qtoken_policy, snapshot.qtokens)}


@pytest.mark.parametrize("label", APP_REDEEMS)
def test_an_app_built_redeem_pays_one_unit_below_the_rate(label):
    fix, snapshot = _redeem(label)
    rate = snapshot.records.state_datum.qtoken_rate
    assert snapshot.payout == redeem_payout(snapshot.qtokens, rate) - 1
    assert captured_mints(fix) == {(snapshot.market.qtoken_policy, -snapshot.qtokens)}


def test_the_floor_redeem_pays_the_full_rate():
    _, snapshot = _redeem("redeem_djed_floor")
    rate = snapshot.records.state_datum.qtoken_rate
    assert snapshot.payout == redeem_payout(snapshot.qtokens, rate)
    assert snapshot.indefinite is True


def test_the_capture_pins_the_app_signer_and_the_window():
    fix, snapshot = _supply("supply_ada")
    assert snapshot.required_signers == tuple(fix["required_signers"])
    assert snapshot.valid_to_ms == slot_to_posix_ms(fix["invalid_hereafter"])
    assert snapshot.valid_from_ms is None
    assert snapshot.indefinite is False


# --- what the builders add and leave out ---------------------------------------


def test_a_supply_signs_nothing_by_default():
    fix, snapshot = _supply("supply_ada")
    built = build(
        build_supply,
        dataclasses.replace(snapshot, required_signers=()),
        slot=_slot(fix),
    )
    assert required_signers(built) == []


def test_a_new_action_datum_is_definite_unless_a_style_is_pinned():
    fix, snapshot = _redeem("redeem_djed_floor")
    built = build(
        build_redeem,
        dataclasses.replace(snapshot, indefinite=None),
        slot=_slot(fix),
    )
    deltas = ActionDeltas(supply=-snapshot.payout, qtoken=-snapshot.qtokens)
    expected = encode_action_datum(deltas.apply_to(snapshot.action.datum))
    assert _action_output_datum(built, snapshot) == expected.hex()


def test_only_the_funding_is_stashed_for_evaluation():
    fix, snapshot = _supply("supply_djed")
    build(build_supply, snapshot, slot=_slot(fix))
    assert snapshot.additional_utxo() == [ogmios_entry(u) for u in snapshot.funding]
    stashed = {(e["transaction"]["id"], e["index"]) for e in snapshot.additional_utxo()}
    assert snapshot.action.utxo.out_ref not in stashed


class _Outer(PoolActionSnapshot):
    pass


def test_the_private_build_stashes_the_funding_where_it_is_told():
    fix, snapshot = _supply("supply_ada")
    outer = _Outer()
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=_slot(fix)))
    _build_supply(tx_builder, snapshot, stash=outer)
    assert outer.additional_utxo() == [ogmios_entry(u) for u in snapshot.funding]
    assert not hasattr(snapshot, "_additional_utxos")


def test_a_pinned_lower_bound_is_written():
    fix, snapshot = _supply("supply_ada")
    lower = snapshot.valid_to_ms - 600_000
    built = build(
        build_supply,
        dataclasses.replace(snapshot, valid_from_ms=lower),
        slot=_slot(fix),
    )
    assert validity(built) == (posix_ms_to_slot(lower), fix["invalid_hereafter"])


def test_an_upper_bound_past_the_batch_deadline_is_refused():
    fix, snapshot = _supply("supply_ada")
    records = snapshot.records
    cap = ttl_cap_ms(records.state_datum, records.params_datum)
    late = dataclasses.replace(snapshot, valid_to_ms=cap + 1_000)
    with pytest.raises(ValidityWindowError, match="batch deadline"):
        build(build_supply, late, slot=_slot(fix))
    built = build(
        build_supply,
        dataclasses.replace(snapshot, valid_to_ms=cap),
        slot=_slot(fix),
    )
    assert validity(built)[1] == posix_ms_to_slot(cap)


def test_a_supply_minting_more_than_the_rate_is_refused():
    fix, snapshot = _supply("supply_ada")
    greedy = dataclasses.replace(snapshot, qtokens=snapshot.qtokens + 1)
    with pytest.raises(ValueError, match="mints at most"):
        build(build_supply, greedy, slot=_slot(fix))


def test_a_redeem_paying_more_than_the_rate_is_refused():
    fix, snapshot = _redeem("redeem_djed_floor")
    greedy = dataclasses.replace(snapshot, payout=snapshot.payout + 1)
    with pytest.raises(ValueError, match="pays at most"):
        build(build_redeem, greedy, slot=_slot(fix))


def test_a_supply_below_the_market_minimum_is_refused_by_the_build():
    fix, snapshot = _supply("supply_ada")
    rate = snapshot.records.state_datum.qtoken_rate
    small = dataclasses.replace(
        snapshot,
        amount=ADA_MIN_VALUE - 1,
        qtokens=supply_mint(ADA_MIN_VALUE - 1, rate),
    )
    with pytest.raises(MinValueError):
        build(build_supply, small, slot=_slot(fix))


def test_a_redeem_beyond_the_action_utxo_is_refused():
    fix, snapshot = _redeem("redeem_djed_floor")
    rate = snapshot.records.state_datum.qtoken_rate
    held = snapshot.action.underlying_amount(snapshot.market.underlying)
    qtokens = redeem_burn_for(held + 1, rate)
    drained = dataclasses.replace(
        snapshot,
        qtokens=qtokens,
        payout=redeem_payout(qtokens, rate),
    )
    with pytest.raises(InsufficientLiquidityError):
        build(build_redeem, drained, slot=_slot(fix))


def _capped(snapshot, cap):
    """``snapshot`` with its MarketParams' supply cap set to ``cap``."""
    records = snapshot.records
    params = dataclasses.replace(records.params_datum, supply_cap=cap)
    record = dataclasses.replace(records.params, datum=params)
    return dataclasses.replace(
        snapshot,
        records=dataclasses.replace(records, params=record),
    )


def test_the_supply_cap_binds_a_supply_but_not_a_redeem():
    fix, supply = _supply("supply_djed")
    with pytest.raises(CapExceededError) as raised:
        build(build_supply, _capped(supply, 1), slot=_slot(fix))
    assert raised.value.cap == "supply"
    fix, redeem = _redeem("redeem_djed_floor")
    built = build(build_redeem, _capped(redeem, 1), slot=_slot(fix))
    assert_replays(built, fix, credentials=CREDENTIALS)


# --- live resolution -----------------------------------------------------------


def _backend(fix: dict[str, Any], *, actions: bool = True, **kwargs) -> FixtureBackend:
    """An offline backend holding a capture's records, scripts and wallet UTxOs.

    The action UTxO and the market records the capture spent or referenced are live
    records (without the action UTxO when ``actions`` is unset); the reference
    scripts and the wallet UTxOs resolve by out-ref.
    """
    utxos = [utxo_from_dict(u) for u in fix["inputs"] + fix["ref_inputs"]]
    records = [
        utxo_to_info(u)
        for u in utxos
        if u.datum and not u.ref_script and (actions or u not in _inputs(fix))
    ]
    resolvable = [u for u in utxos if u.ref_script or not u.datum]
    return FixtureBackend(
        records=records,
        utxos=resolvable,
        tip_time=kwargs.pop("tip_time", fix["block_time"]),
    )


def _inputs(fix: dict[str, Any]):
    return [utxo_from_dict(u) for u in fix["inputs"]]


def _wallet(fix: dict[str, Any]) -> str:
    return next(u["address"] for u in fix["inputs"] if not u["datum"])


def test_a_live_supply_builds_the_captured_protocol_effects():
    fix = fixture("supply_ada")
    snapshot = SupplySnapshot.from_backend(
        _backend(fix),
        market="Ada",
        owner_address=_wallet(fix),
        amount=100_000_000,
        rng=random.Random(0),
    )
    built = build(build_supply, snapshot, slot=_slot(fix))
    assert redeemers(built) == captured_redeemers(fix)
    assert protocol_outputs(built, CREDENTIALS) == captured_protocol_outputs(
        fix,
        CREDENTIALS,
    )
    assert mints(built) == captured_mints(fix)
    assert required_signers(built) == []
    assert snapshot.funding == [
        u for u in _inputs(fix) if u.out_ref != snapshot.action.utxo.out_ref
    ]
    records = snapshot.records
    cap = ttl_cap_ms(records.state_datum, records.params_datum)
    assert snapshot.valid_to_ms == min(fix["block_time"] * 1000 + 900_000, cap - 1_000)


def test_a_live_supply_honours_a_requested_bound_and_funding():
    fix = fixture("supply_ada")
    wallet = [u for u in _inputs(fix) if not u.datum]
    requested = slot_to_posix_ms(fix["invalid_hereafter"])
    snapshot = SupplySnapshot.from_backend(
        _backend(fix),
        market="Ada",
        owner_address=_wallet(fix),
        amount=100_000_000,
        valid_to_ms=requested,
        funding=wallet,
    )
    assert snapshot.funding == wallet
    assert snapshot.valid_to_ms == requested


def test_a_live_supply_below_the_minimum_is_refused_before_any_action_choice():
    fix = fixture("supply_ada")
    with pytest.raises(MinValueError) as raised:
        SupplySnapshot.from_backend(
            _backend(fix, actions=False),
            market="Ada",
            owner_address=_wallet(fix),
            amount=ADA_MIN_VALUE - 1,
        )
    assert (raised.value.amount, raised.value.minimum) == (
        ADA_MIN_VALUE - 1,
        ADA_MIN_VALUE,
    )


def test_a_live_supply_at_the_batch_deadline_waits_for_the_batch():
    fix, snapshot = _supply("supply_ada")
    records = snapshot.records
    cap = ttl_cap_ms(records.state_datum, records.params_datum)
    with pytest.raises(MarketAwaitingBatchError):
        SupplySnapshot.from_backend(
            _backend(fix, tip_time=cap // 1000),
            market="Ada",
            owner_address=_wallet(fix),
            amount=100_000_000,
        )


def test_a_live_redeem_of_an_amount_burns_the_fewest_qtokens():
    fix = fixture("redeem_ada")
    snapshot = RedeemSnapshot.from_backend(
        _backend(fix),
        market="Ada",
        owner_address=_wallet(fix),
        amount=99_999_998,
        rng=random.Random(0),
    )
    rate = snapshot.records.state_datum.qtoken_rate
    assert snapshot.qtokens == redeem_burn_for(99_999_998, rate)
    assert snapshot.payout == redeem_payout(snapshot.qtokens, rate) >= 99_999_998
    built = build(build_redeem, snapshot, slot=_slot(fix))
    assert mints(built) == {(QADA, -snapshot.qtokens)}
    assert redeemers(built) == captured_redeemers(fix)
    assert required_signers(built) == []


def test_withdraw_all_burns_the_funded_qtoken_balance():
    fix = fixture("redeem_ada")
    snapshot = RedeemSnapshot.from_backend(
        _backend(fix),
        market="Ada",
        owner_address=_wallet(fix),
        withdraw_all=True,
    )
    held = sum(
        qty
        for u in snapshot.funding
        for policy, name, qty in u.assets
        if (policy, name) == (QADA, "")
    )
    assert held == 439_021_935_878
    rate = snapshot.records.state_datum.qtoken_rate
    assert snapshot.qtokens == held
    assert snapshot.payout == redeem_payout(held, rate)
    built = build(build_redeem, snapshot, slot=_slot(fix))
    assert mints(built) == {(QADA, -held)}


def test_withdraw_all_without_qtokens_is_refused():
    fix = fixture("supply_ada")
    with pytest.raises(ValueError, match="holds no qTokens"):
        RedeemSnapshot.from_backend(
            _backend(fix),
            market="Ada",
            owner_address=_wallet(fix),
            withdraw_all=True,
        )


def test_a_redeem_beyond_the_funded_qtokens_is_refused():
    fix = fixture("redeem_ada")
    with pytest.raises(ValueError, match="qTokens"):
        RedeemSnapshot.from_backend(
            _backend(fix),
            market="Ada",
            owner_address=_wallet(fix),
            amount=10**13,
        )


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"amount": 99_999_998, "withdraw_all": True}],
    ids=["neither", "both"],
)
def test_a_redeem_takes_an_amount_or_withdraw_all(kwargs):
    fix = fixture("redeem_ada")
    with pytest.raises(ValueError, match="exactly one"):
        RedeemSnapshot.from_backend(
            _backend(fix),
            market="Ada",
            owner_address=_wallet(fix),
            **kwargs,
        )


def test_a_live_redeem_below_the_minimum_is_refused():
    fix = fixture("redeem_ada")
    with pytest.raises(MinValueError):
        RedeemSnapshot.from_backend(
            _backend(fix, actions=False),
            market="Ada",
            owner_address=_wallet(fix),
            amount=ADA_MIN_VALUE - 2,
        )
