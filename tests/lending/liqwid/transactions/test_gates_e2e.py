"""Ogmios gates for the Liqwid V2 builders.

Every captured transaction is rebuilt from its snapshot and evaluated by Ogmios,
the captured inputs and reference inputs supplied as ``additionalUtxo`` (the ones
Ogmios still holds are resolved from its ledger). Each negative gate builds a
capture, changes one thing on the builder after the flow's own checks, and expects
the Liqwid scripts to refuse the transaction (Ogmios script failure, code 3010).

The remaining gates measure what the scripts accept where the protocol rules
were inferred rather than observed; each docstring records the result.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from fractions import Fraction
from typing import Any

import cbor2
import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.liqwid.datums import decode_oracle_feed
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions import legs
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import build_increase
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import build_collateral
from charli3_dendrite.lending.liqwid.transactions.combos import (
    RemoveAndRedeemSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    SupplyAndBorrowSnapshot,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    build_remove_and_redeem,
)
from charli3_dendrite.lending.liqwid.transactions.combos import (
    build_supply_and_borrow,
)
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.repay import build_repay
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import build_supply
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_burn_for
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_payout
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.errors import OgmiosEvaluationError
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from tests.lending.liqwid.transactions.replay import Mutation
from tests.lending.liqwid.transactions.replay import add_reference
from tests.lending.liqwid.transactions.replay import block_slot
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import captured_purposes
from tests.lending.liqwid.transactions.replay import drop_required_signers
from tests.lending.liqwid.transactions.replay import evaluate
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import fixture_utxos
from tests.lending.liqwid.transactions.replay import keep_loan_token
from tests.lending.liqwid.transactions.replay import labels
from tests.lending.liqwid.transactions.replay import mutated
from tests.lending.liqwid.transactions.replay import needs_ogmios
from tests.lending.liqwid.transactions.replay import replace_reference
from tests.lending.liqwid.transactions.replay import set_upper_bound_ms
from tests.lending.liqwid.transactions.replay import shift_action_datum
from tests.lending.liqwid.transactions.replay import shift_loan_datum
from tests.lending.liqwid.transactions.replay import shift_value
from tests.lending.liqwid.transactions.replay import synthetic_utxo
from tests.lending.liqwid.transactions.replay import with_params
from tests.lending.liqwid.transactions.test_supply import _redeem

pytestmark = needs_ogmios

# Ogmios: some scripts of the transaction failed.
SCRIPT_FAILURE = 3010
# The script that checks a flow's validity window: the borrow staking script on a
# loan flow, the qToken policy on a supply.
CHECKED_BY = {"increase_ada": ["withdraw"], "supply_ada": ["mint"]}

SUPPLIES = (
    "supply_ada",
    "supply_djed",
    "family_isolated_supply",
    "family_collateral_only_supply",
    "family_staked_supply",
)
REDEEMS = (
    "redeem_ada",
    "redeem_djed_floor",
    "family_isolated_redeem",
    "family_lp_redeem",
)
OPENS = (
    "open_single",
    "open_three",
    "open_liquidation_first",
    "open_at_limit",
    "open_indefinite",
    "family_isolated_open",
)
INCREASES = (
    "increase_ada",
    "increase_usdm",
    "increase_with_collateral",
    "family_lp_increase",
    "family_staked_increase",
)
REPAYS = (
    "repay_usdm",
    "repay_clamp",
    "repay_interest_only",
    "repay_with_collateral",
    "family_isolated_repay",
    "family_lp_repay",
    "close_ada",
    "close_top_up",
    "close_zero_debt",
    "family_isolated_close",
    "family_lp_close",
    "family_staked_close",
)
COLLATERAL_CHANGES = (
    "collateral_add",
    "collateral_multi_add",
    "collateral_five",
    "collateral_remove",
    "collateral_remove_two",
    "collateral_drop_policy",
    "collateral_rebase",
    "family_isolated_collateral",
    "family_lp_collateral",
)
SUPPLY_AND_BORROWS = ("supply_and_borrow_ada_night", "supply_and_borrow_usdt_usdc")
REMOVE_AND_REDEEMS = ("remove_and_redeem_a", "remove_and_redeem_night")

FLOWS: dict[str, tuple[Any, Callable[..., None]]] = {
    **{label: (SupplySnapshot, build_supply) for label in SUPPLIES},
    **{label: (RedeemSnapshot, build_redeem) for label in REDEEMS},
    **{label: (BorrowSnapshot, build_borrow) for label in OPENS},
    **{label: (IncreaseSnapshot, build_increase) for label in INCREASES},
    **{label: (RepaySnapshot, build_repay) for label in REPAYS},
    **{label: (CollateralSnapshot, build_collateral) for label in COLLATERAL_CHANGES},
    **{
        label: (SupplyAndBorrowSnapshot, build_supply_and_borrow)
        for label in SUPPLY_AND_BORROWS
    },
    **{
        label: (RemoveAndRedeemSnapshot, build_remove_and_redeem)
        for label in REMOVE_AND_REDEEMS
    },
}

QBTC = "f72166e9fac8297aeb553c19ffab14f51ae271c2cb26783ba289a3a5"
QUSDM = "9e00df0615de0a7b121a7f961d43e23165b8e81b64786c6eb708d370"
# A key no Liqwid record names: the owner of the made-up wallet UTxOs below.
STRANGER = str(
    Address(payment_part=VerificationKeyHash(b"\x0b" * 28), network=Network.MAINNET),
)


# --- helpers -------------------------------------------------------------------


def _capture(label: str) -> tuple[dict[str, Any], Any, Callable[..., None]]:
    """A capture, its snapshot and its flow's build function.

    A redeem that ran the qToken policy from its witness set gets that policy's
    reference UTxO among its references (as in the replays).
    """
    cls, build_flow = FLOWS[label]
    if cls is RedeemSnapshot:
        fix, snapshot = _redeem(label)
    else:
        fix = fixture(label)
        snapshot = cls.from_capture(fix)
    return fix, snapshot, build_flow


def _purposes(
    label: str,
    *mutations: Mutation,
    snapshot: Any = None,  # noqa: ANN401
    extra: tuple[Utxo, ...] = (),
) -> list[str]:
    """Evaluate ``label`` rebuilt (from ``snapshot`` when given) and mutated."""
    fix, captured, build_flow = _capture(label)
    built = build(
        mutated(build_flow, *mutations),
        captured if snapshot is None else snapshot,
        slot=block_slot(fix),
    )
    return evaluate(built, [*fixture_utxos(fix), *extra])


def _refused(
    label: str,
    *mutations: Mutation,
    snapshot: Any = None,  # noqa: ANN401
    extra: tuple[Utxo, ...] = (),
) -> list[str]:
    """Assert the scripts refuse ``label`` rebuilt and mutated.

    Returns:
        The sorted purposes of the scripts that failed: ``withdraw`` is the borrow
        staking script, which checks every loan flow; ``mint`` on a supply or
        redeem is the qToken policy.
    """
    with pytest.raises(OgmiosEvaluationError) as info:
        _purposes(label, *mutations, snapshot=snapshot, extra=extra)
    error = info.value
    assert error.code == SCRIPT_FAILURE, error
    return sorted(failure["validator"]["purpose"] for failure in error.data)


def _params(snapshot: Any) -> Any:  # noqa: ANN401
    return snapshot.records.params_datum


def _cap_ms(snapshot: Any) -> int:  # noqa: ANN401
    """The batch deadline ``s8 + p[3]`` of the snapshot's market."""
    return ttl_cap_ms(snapshot.records.state_datum, _params(snapshot))


def _action_credential(snapshot: Any) -> str:  # noqa: ANN401
    return _params(snapshot).action_validator


def _loan_credential(snapshot: Any) -> str:  # noqa: ANN401
    return _params(snapshot).loan_validator


def _without_min_value(snapshot: Any) -> Any:  # noqa: ANN401
    """``snapshot`` with the builder's view of ``p[1]`` lowered to zero.

    The transaction still references the market's real MarketParams, so the
    scripts check the real minimum.
    """
    raw = list(_params(snapshot).raw)
    raw[1] = 0
    return with_params(snapshot, raw=tuple(raw))


def _wallet_utxo(seed: str, address: str = STRANGER) -> Utxo:
    """A plain wallet UTxO no ledger holds: 5 ADA, no token, no datum."""
    return synthetic_utxo(seed, address=address, lovelace=5_000_000)


@pytest.fixture
def no_builder_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build loans the builder would refuse as unhealthy, for the scripts to judge."""
    monkeypatch.setattr(legs, "check_loan_health", lambda *args, **kwargs: None)


# --- positive gates ------------------------------------------------------------


def test_every_capture_is_gated():
    assert set(FLOWS) == set(labels())


@pytest.mark.parametrize("label", sorted(FLOWS))
def test_a_rebuilt_capture_evaluates(label):
    """The rebuilt transaction runs exactly the captured scripts, each in budget."""
    assert _purposes(label) == captured_purposes(fixture(label))


# --- validity ------------------------------------------------------------------


@pytest.mark.parametrize("label", ["increase_ada", "supply_ada"])
def test_an_upper_bound_at_the_batch_deadline_evaluates(label):
    _, snapshot, _ = _capture(label)
    assert _purposes(label, set_upper_bound_ms(_cap_ms(snapshot)))


@pytest.mark.parametrize("label", ["increase_ada", "supply_ada"])
def test_an_upper_bound_a_second_past_the_batch_deadline_is_refused(label):
    _, snapshot, _ = _capture(label)
    failed = _refused(label, set_upper_bound_ms(_cap_ms(snapshot) + 1_000))
    assert failed == CHECKED_BY[label]


@pytest.mark.parametrize("label", ["increase_ada", "supply_ada"])
def test_no_upper_bound_is_refused(label):
    assert _refused(label, set_upper_bound_ms(None)) == CHECKED_BY[label]


# --- records -------------------------------------------------------------------


@pytest.mark.parametrize("field", ["index", "interest", "min_interest"])
@pytest.mark.parametrize("by", [-1, 1])
def test_a_loan_record_off_by_one_is_refused(field, by):
    _, snapshot, _ = _capture("increase_ada")
    shift = shift_loan_datum(_loan_credential(snapshot), **{field: by})
    assert _refused("increase_ada", shift) == ["withdraw"]


@pytest.mark.parametrize("field", ["supply_delta", "qtoken_delta"])
@pytest.mark.parametrize("by", [-1, 1])
def test_an_action_delta_off_by_one_is_refused(field, by):
    _, snapshot, _ = _capture("supply_ada")
    shift = shift_action_datum(_action_credential(snapshot), **{field: by})
    assert _refused("supply_ada", shift) == ["mint"]


def test_one_more_paid_into_the_action_on_a_close_is_refused():
    _, snapshot, _ = _capture("close_ada")
    assert snapshot.closes
    underlying = snapshot.market.underlying
    shift = (
        shift_value(_action_credential(snapshot), lovelace=1)
        if underlying == "lovelace"
        else shift_value(_action_credential(snapshot), assets={underlying: 1})
    )
    assert _refused("close_ada", shift) == ["withdraw"]


def test_a_loan_lowered_by_one_lovelace_is_refused():
    """The borrow-more keeps the input loan's lovelace; one less is refused."""
    _, snapshot, _ = _capture("increase_ada")
    shift = shift_value(_loan_credential(snapshot), lovelace=-1)
    assert _refused("increase_ada", shift) == ["withdraw"]


@pytest.mark.parametrize("label", ["increase_ada", "collateral_add", "close_ada"])
def test_a_loan_flow_without_the_owner_signature_is_refused(label):
    assert _refused(label, drop_required_signers) == ["withdraw"]


def test_a_modification_that_keeps_the_loan_token_name_is_refused():
    _, snapshot, _ = _capture("increase_ada")
    keep = keep_loan_token(snapshot.market.new_borrow_policy)
    assert _refused("increase_ada", keep) == ["withdraw"]


def test_a_token_that_is_no_qtoken_in_the_loan_is_refused():
    _, snapshot, _ = _capture("increase_ada")
    djed = default_deployment().market_by_name("DJED").underlying
    shift = shift_value(_loan_credential(snapshot), assets={djed: 1})
    assert _refused("increase_ada", shift) == ["withdraw"]


# --- health --------------------------------------------------------------------


def test_a_collateral_params_record_dropped_from_the_health_set_is_refused():
    _, snapshot, _ = _capture("collateral_remove")
    record = utxo_from_info(snapshot.health.cparams[QBTC].info)
    assert record.out_ref is not None
    drop = replace_reference(record.out_ref, None)
    assert _refused("collateral_remove", drop) == ["withdraw"]


@pytest.mark.usefixtures("no_builder_health_check")
@pytest.mark.parametrize(
    ("label", "unit", "failing"),
    [
        ("collateral_remove", QBTC, 1_371_905_771),
        ("collateral_remove_two", QUSDM, 116_890_345_444),
    ],
)
def test_a_removal_is_held_to_the_exact_health_boundary(label, unit, failing):
    """The builder's health boundary is the scripts' to the base unit."""
    _, snapshot, _ = _capture(label)
    for quantity, accepted in [(failing, False), (failing + 1, True)]:
        target = {**snapshot.collateral, unit: quantity}
        changed = dataclasses.replace(snapshot, collateral=target)
        if accepted:
            assert _purposes(label, snapshot=changed)
        else:
            assert _refused(label, snapshot=changed) == ["withdraw"]


def _priced_feed(feed: Utxo, price: Fraction, seed: str) -> Utxo:
    """A copy of the oracle ``feed`` pricing its asset at ``price``, at a new out-ref."""
    assert feed.datum is not None
    fields = cbor2.loads(bytes.fromhex(feed.datum))
    fields[2] = [price.numerator, price.denominator]
    datum = cbor2.dumps(fields).hex()
    assert decode_oracle_feed(datum).price == (price.numerator, price.denominator)
    copy = synthetic_utxo(
        seed,
        address=feed.address,
        lovelace=feed.lovelace,
        assets=feed.assets,
        datum=datum,
    )
    return dataclasses.replace(copy, ref_script=None)


@pytest.mark.usefixtures("no_builder_health_check")
@pytest.mark.parametrize(
    ("price", "accepted"),
    [(695_635_307, False), (695_635_308, True)],
)
def test_an_add_is_held_to_the_health_rule_at_the_collateral_price(price, accepted):
    """A collateral add is checked at the collateral's feed price, to the unit."""
    _, snapshot, _ = _capture("collateral_add")
    underlying = snapshot.health.cparams[QBTC].datum.underlying
    feed = utxo_from_info(snapshot.health.feeds[underlying].info)
    assert feed.out_ref is not None
    priced = _priced_feed(feed, Fraction(price, 1_000_000), f"feed-{price}")
    swap = replace_reference(feed.out_ref, priced)
    if accepted:
        assert _purposes("collateral_add", swap, extra=(priced,))
    else:
        assert _refused("collateral_add", swap, extra=(priced,)) == ["withdraw"]


# --- inferred rules -------------------------------------------------------------


def test_a_remove_and_redeem_is_bounded_by_its_redeem_market_only():
    """A remove-and-redeem's upper bound is checked against the redeemed market.

    On the captured NIGHT loan whose qAda is redeemed on the Ada market, an upper
    bound one second past NIGHT's batch deadline, and inside Ada's, evaluates: the
    loan leg spends no action UTxO and checks no deadline, so only the redeem
    market's deadline binds the transaction.
    """
    _, snapshot, _ = _capture("remove_and_redeem_night")
    loan_cap = _cap_ms(snapshot.collateral)
    redeem_cap = _cap_ms(snapshot.redeem)
    upper = loan_cap + 1_000
    assert upper < redeem_cap
    assert _purposes("remove_and_redeem_night", set_upper_bound_ms(upper))
    past = set_upper_bound_ms(redeem_cap + 1_000)
    assert _refused("remove_and_redeem_night", past) == ["mint"]


@pytest.mark.parametrize("label", ["supply_ada", "redeem_ada"])
def test_a_wallet_reference_input_passes_the_action_validator(label):
    """The action validator and the qToken policy accept any reference input.

    A plain wallet UTxO referenced by a supply or a redeem evaluates, so the
    reference-input hygiene check stays on the loan flows, whose validator refuses
    the same reference (below).
    """
    wallet = _wallet_utxo(f"wallet-reference-{label}")
    assert _purposes(label, add_reference(wallet), extra=(wallet,))


def test_a_wallet_reference_input_is_refused_on_a_loan_flow():
    wallet = _wallet_utxo("wallet-reference-increase_ada")
    failed = _refused("increase_ada", add_reference(wallet), extra=(wallet,))
    assert failed == ["withdraw"]


def test_an_open_below_the_market_minimum_is_refused():
    """An open of ``p[1] - 1`` is refused and an open of ``p[1]`` evaluates."""
    _, snapshot, _ = _capture("open_single")
    minimum = _params(snapshot).min_value
    unchecked = _without_min_value(snapshot)
    at = dataclasses.replace(unchecked, amount=minimum)
    below = dataclasses.replace(unchecked, amount=minimum - 1)
    assert _purposes("open_single", snapshot=at)
    assert _refused("open_single", snapshot=below) == ["withdraw"]


def test_a_whole_balance_redeem_below_the_market_minimum_is_refused():
    """Redeeming every qToken a wallet holds is held to ``p[1]`` like any redeem.

    The wallet holds exactly the qTokens burnt. A burn paying ``p[1] - 1`` or less
    is refused and the smallest burn paying at least ``p[1]`` evaluates, so a
    holder of less than ``p[1]`` of underlying cannot redeem it.
    """
    _, snapshot, _ = _capture("redeem_ada")
    rate = snapshot.records.state_datum.qtoken_rate
    minimum = _params(snapshot).min_value
    enough = redeem_burn_for(minimum, rate)
    for qtokens, accepted in [(enough - 1, False), (enough, True)]:
        payout = redeem_payout(qtokens, rate)
        assert (payout >= minimum) is accepted
        wallet = synthetic_utxo(
            f"whole-balance-{qtokens}",
            address=snapshot.owner_address,
            lovelace=5_000_000,
            assets=[(snapshot.market.qtoken_policy, "", qtokens)],
        )
        whole = dataclasses.replace(
            _without_min_value(snapshot),
            qtokens=qtokens,
            payout=payout,
            funding=[wallet],
        )
        if accepted:
            assert _purposes("redeem_ada", snapshot=whole, extra=(wallet,))
        else:
            failed = _refused("redeem_ada", snapshot=whole, extra=(wallet,))
            assert failed == ["mint"]
