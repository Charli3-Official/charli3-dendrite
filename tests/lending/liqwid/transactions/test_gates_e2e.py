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
from charli3_dendrite.lending.liqwid.transactions.borrow import _build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import build_increase
from charli3_dendrite.lending.liqwid.transactions.builder import LiqwidTxBuilder
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import _build_collateral
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
from charli3_dendrite.lending.liqwid.transactions.errors import ActionUtxoSpentError
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.repay import build_repay
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import _build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import _build_supply
from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import build_supply
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_burn_for
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_payout
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.base import ActionParams
from charli3_dendrite.lending.transactions.base import LendingAction
from charli3_dendrite.lending.transactions.errors import OgmiosEvaluationError
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.lending.units import out_ref_text
from tests.lending.liqwid.records import parsed_records
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.backend import capture_backend
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
from tests.lending.liqwid.transactions.test_resolve import _reference_utxos
from tests.lending.liqwid.transactions.test_resolve import (  # noqa: F401 - fixture
    deployment_hashes,
)
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


# --- a spent input ---------------------------------------------------------------


def _withheld_input(label: str, role: str) -> str:
    """The out-ref of ``label``'s ``role`` input: its action UTxO, loan or wallet."""
    _, snapshot, _ = _capture(label)
    if role == "action":
        return snapshot.action.out_ref
    if role == "loan":
        return out_ref_text(snapshot.loan.utxo.out_ref)
    return out_ref_text(snapshot.funding[0].out_ref)


@pytest.mark.parametrize(
    ("label", "role"),
    [
        ("supply_ada", "action"),
        ("supply_ada", "wallet"),
        ("increase_ada", "action"),
        ("increase_ada", "loan"),
    ],
)
def test_an_input_missing_from_the_utxo_set_is_named(label, role):
    """An input Ogmios does not hold is read back from its error.

    The capture is evaluated with every captured UTxO but one supplied. Ogmios
    answers with a script failure (3010) whose per-validator errors carry a 3004
    naming the input "missing from UTxO set"; `missing_inputs` reads exactly it.
    """
    fix, snapshot, build_flow = _capture(label)
    withheld = _withheld_input(label, role)
    built = build(build_flow, snapshot, slot=block_slot(fix))
    utxos = [u for u in fixture_utxos(fix) if out_ref_text(u.out_ref) != withheld]
    with pytest.raises(OgmiosEvaluationError) as info:
        evaluate(built, utxos)
    assert info.value.code == SCRIPT_FAILURE, info.value
    assert {out_ref_text(ref) for ref in info.value.missing_inputs()} == {withheld}


def test_a_spent_action_utxo_raises_action_utxo_spent_error():
    """`build_and_evaluate` names a chosen action UTxO the ledger no longer holds.

    The supply is resolved from the capture's records and evaluated as a live
    build is: only the wallet funding is supplied. Ogmios holds no copy of the
    captured action UTxO, long spent by a batch, and the evaluation maps to
    `ActionUtxoSpentError` naming it.
    """
    fix = fixture("supply_ada")
    _, snapshot, _ = _capture("supply_ada")
    wallet = snapshot.funding[0].address
    builder = LiqwidTxBuilder()
    with pytest.raises(ActionUtxoSpentError) as info:
        builder.build_and_evaluate(
            backend=capture_backend(fix),
            market_name="Ada",
            action=LendingAction.DEPOSIT,
            params=ActionParams(actor_address=wallet, amount=100_000_000),
        )
    assert info.value.out_ref == snapshot.action.out_ref
    cause = info.value.__cause__
    assert isinstance(cause, OgmiosEvaluationError)
    assert cause.code == SCRIPT_FAILURE


# --- one action UTxO per market ----------------------------------------------------

# Two Ada action UTxOs one batch created, both reading one Ada MarketState: the
# action inputs of these captures (``#4`` and ``#5`` of one transaction).
SUPPLY_SIDE = "supply_ada"
BORROW_SIDE = "increase_ada"
ADA_SUPPLY = 1_000_000_000
ADA_BORROW = 100_000_000
WALLET_QADA = 47_000_000_000
LOAN_QADA = 100_000_000_000
REMOVED_QADA = 20_000_000_000


def _ada() -> Any:  # noqa: ANN401
    return default_deployment().market_by_name("Ada")


def _ada_action(label: str) -> str:
    (action,) = CapturedTx.of(fixture(label)).action_inputs(_ada())
    return action.out_ref


def _ada_cparams_for_qada() -> Any:  # noqa: ANN401
    """The live Ada CollateralParams record of qAda."""
    ada = _ada()
    (info,) = [
        record.info
        for _, record in parsed_records("collateral_params", live=True)
        if record.market_id == ada.market_id
        and getattr(record.datum, "collateral", None) == ada.qtoken_policy
        and not record.datum.policy_wide
    ]
    return info


def _one_market_fixtures() -> list[dict[str, Any]]:
    return [fixture(SUPPLY_SIDE), fixture(BORROW_SIDE)]


def _one_market_backend(*utxos: Utxo) -> FixtureBackend:
    """Ada's records, scripts and feed, both Ada action UTxOs, and ``utxos``."""
    supply_side, borrow_side = _one_market_fixtures()
    refs = [utxo_from_dict(u) for u in borrow_side["ref_inputs"]]
    held = {u.out_ref for u in refs}
    scripts = [u for u in refs if u.ref_script] + [
        u
        for u in (utxo_from_dict(r) for r in supply_side["ref_inputs"])
        if u.ref_script and u.out_ref not in held
    ]
    script_refs = {u.out_ref for u in scripts}
    stand_ins = [u for u in _reference_utxos() if u.out_ref not in script_refs]
    actions = [
        a.utxo
        for fix in (supply_side, borrow_side)
        for a in CapturedTx.of(fix).action_inputs(_ada())
    ]
    records = [u for u in refs if u.datum and not u.ref_script] + actions
    return FixtureBackend(
        records=[*(utxo_to_info(u) for u in records), _ada_cparams_for_qada()],
        utxos=[*scripts, *stand_ins, *utxos],
        tip_time=max(fix["block_time"] for fix in (supply_side, borrow_side)),
    )


def _one_market_wallet() -> list[Utxo]:
    return [
        u
        for fix in _one_market_fixtures()
        for u in CapturedTx.of(fix).inputs
        if not u.datum
    ]


def _qada_wallet(seed: str, owner: str, qtokens: int) -> Utxo:
    """A wallet UTxO no ledger holds, with 5 ADA and ``qtokens`` qAda."""
    return synthetic_utxo(
        seed,
        address=owner,
        lovelace=5_000_000,
        assets=[(_ada().qtoken_policy, "", qtokens)],
    )


def _ada_loan_holding_qada() -> Utxo:
    """increase_ada's Ada loan holding only ``LOAN_QADA`` qAda, at a new out-ref."""
    (loan,) = CapturedTx.of(fixture(BORROW_SIDE)).loan_inputs()
    token = [a for a in loan.utxo.assets if a[0] == _ada().new_borrow_policy]
    assets = sorted([*token, (_ada().qtoken_policy, "", LOAN_QADA)])
    return dataclasses.replace(loan.utxo, assets=assets, out_ref=("ee" * 32, 0))


def _one_market_evaluated(
    contribute: Callable[..., None],
    snapshot: Any,  # noqa: ANN401
    extra: tuple[Utxo, ...] = (),
) -> list[str]:
    """Evaluate ``snapshot`` built at the later capture's slot, all UTxOs supplied."""
    fixes = _one_market_fixtures()
    built = build(contribute, snapshot, slot=max(block_slot(f) for f in fixes))
    cparams = utxo_from_info(_ada_cparams_for_qada())
    utxos = [*(u for f in fixes for u in fixture_utxos(f)), cparams, *extra]
    return evaluate(built, utxos)


def _script_refused(
    contribute: Callable[..., None],
    snapshot: Any,  # noqa: ANN401
    extra: tuple[Utxo, ...] = (),
) -> list[str]:
    """The sorted purposes of the scripts that refuse ``snapshot``'s build."""
    with pytest.raises(OgmiosEvaluationError) as info:
        _one_market_evaluated(contribute, snapshot, extra)
    assert info.value.code == SCRIPT_FAILURE, info.value
    return sorted(failure["validator"]["purpose"] for failure in info.value.data)


def _supply(action_utxo: str) -> SupplySnapshot:
    wallet = _one_market_wallet()
    return SupplySnapshot.from_backend(
        _one_market_backend(),
        market="Ada",
        owner_address=wallet[0].address,
        amount=ADA_SUPPLY,
        action_utxo=action_utxo,
        funding=wallet,
    )


def _open(action_utxo: str, qtokens: int, *extra: Utxo) -> BorrowSnapshot:
    wallet = _one_market_wallet()
    return BorrowSnapshot.from_backend(
        _one_market_backend(),
        market="Ada",
        owner_address=wallet[0].address,
        amount=ADA_BORROW,
        collateral={_ada().qtoken_policy: qtokens},
        action_utxo=action_utxo,
        funding=[*wallet, *extra],
    )


def _removal(loan: Utxo) -> CollateralSnapshot:
    wallet = _one_market_wallet()
    return CollateralSnapshot.from_backend(
        _one_market_backend(loan),
        loan_utxo=out_ref_text(loan.out_ref),
        owner_address=wallet[0].address,
        collateral={_ada().qtoken_policy: LOAN_QADA - REMOVED_QADA},
        funding=wallet,
    )


def _redeem_of(action_utxo: str, funding: list[Utxo]) -> RedeemSnapshot:
    return RedeemSnapshot.from_backend(
        _one_market_backend(),
        market="Ada",
        owner_address=funding[0].address,
        withdraw_all=True,
        action_utxo=action_utxo,
        funding=funding,
    )


def _supply_then_open(
    tx_builder: Any,  # noqa: ANN401
    *,
    snapshot: SupplyAndBorrowSnapshot,
) -> None:
    """Both legs of a supply-and-borrow, without the combination's market check."""
    _build_supply(tx_builder, snapshot.supply, stash=snapshot)
    _build_borrow(tx_builder, snapshot.borrow, stash=snapshot)


def _redeem_then_remove(
    tx_builder: Any,  # noqa: ANN401
    *,
    snapshot: RemoveAndRedeemSnapshot,
) -> None:
    """Both legs of a remove-and-redeem, without the combination's market check."""
    _build_redeem(tx_builder, snapshot.redeem, stash=snapshot)
    _build_collateral(tx_builder, snapshot.collateral, stash=snapshot)


@pytest.mark.usefixtures("deployment_hashes")
def test_each_one_market_leg_evaluates_alone():
    """Each leg of the refused one-market combinations evaluates on its own.

    A supply of 1,000 ADA on one Ada action UTxO; an open of 100 ADA on the other,
    its qAda collateral from a wallet UTxO; 20e9 qAda taken out of an Ada loan
    holding 100e9; a redeem of 20e9 qAda on an Ada action UTxO.
    """
    supply_pin, borrow_pin = _ada_action(SUPPLY_SIDE), _ada_action(BORROW_SIDE)
    owner = _one_market_wallet()[0].address
    collateral = _qada_wallet("one-market-collateral", owner, WALLET_QADA)
    redeemed = _qada_wallet("one-market-redeemed", owner, REMOVED_QADA)
    loan = _ada_loan_holding_qada()
    assert _one_market_evaluated(build_supply, _supply(supply_pin)) == [
        "mint",
        "spend",
    ]
    opened = _open(borrow_pin, WALLET_QADA, collateral)
    assert _one_market_evaluated(build_borrow, opened, (collateral,)) == [
        "mint",
        "spend",
        "withdraw",
    ]
    assert _one_market_evaluated(build_collateral, _removal(loan), (loan,)) == [
        "mint",
        "spend",
        "withdraw",
    ]
    redeem = _redeem_of(borrow_pin, [*_one_market_wallet(), redeemed])
    assert redeem.qtokens == REMOVED_QADA
    assert _one_market_evaluated(build_redeem, redeem, (redeemed,)) == [
        "mint",
        "spend",
    ]


@pytest.mark.usefixtures("deployment_hashes")
@pytest.mark.parametrize("swapped", [False, True], ids=["as-captured", "swapped"])
def test_a_supply_and_borrow_on_one_market_is_refused_by_the_scripts(swapped):
    """Two action UTxOs of one market in one transaction are refused.

    The supply on one Ada action UTxO and the open on the other, its collateral the
    qAda the supply mints, each evaluate alone (above). Together both action
    spends, the qAda policy and the borrow staking script fail; only the loan-token
    policy passes. This is why `SupplyAndBorrowSnapshot` refuses one market.
    """
    supply_pin, borrow_pin = _ada_action(SUPPLY_SIDE), _ada_action(BORROW_SIDE)
    if swapped:
        supply_pin, borrow_pin = borrow_pin, supply_pin
    supply = _supply(supply_pin)
    borrow = _open(borrow_pin, supply.qtokens)
    combined = SupplyAndBorrowSnapshot(supply=supply, borrow=borrow)
    failed = _script_refused(_supply_then_open, combined)
    assert failed == ["mint", "spend", "spend", "withdraw"]


@pytest.mark.usefixtures("deployment_hashes")
def test_a_supply_and_an_open_of_one_market_are_refused_without_the_mint():
    """The refusal does not depend on the open's collateral coming from the mint.

    The open's qAda come from a wallet UTxO, as in the open alone; the supply and
    the open on two Ada action UTxOs are still refused together.
    """
    supply_pin, borrow_pin = _ada_action(SUPPLY_SIDE), _ada_action(BORROW_SIDE)
    owner = _one_market_wallet()[0].address
    collateral = _qada_wallet("one-market-collateral", owner, WALLET_QADA)
    combined = SupplyAndBorrowSnapshot(
        supply=_supply(supply_pin),
        borrow=_open(borrow_pin, WALLET_QADA, collateral),
    )
    failed = _script_refused(_supply_then_open, combined, (collateral,))
    assert failed == ["mint", "spend", "spend", "withdraw"]


@pytest.mark.usefixtures("deployment_hashes")
def test_a_remove_and_redeem_on_the_loans_market_is_refused_by_the_scripts():
    """An action UTxO spent next to a collateral change of its market's loan.

    20e9 qAda taken out of an Ada loan and redeemed on an Ada action UTxO: each leg
    evaluates alone (above), and together the Ada action validator and the borrow
    staking script fail. This is why `RemoveAndRedeemSnapshot` refuses the loan
    market's own qToken.
    """
    loan = _ada_loan_holding_qada()
    removal = _removal(loan)
    redeem = RedeemSnapshot._from_records(
        _one_market_backend(),
        removal.records,
        owner_address=removal.owner_address,
        qtokens=REMOVED_QADA,
        funding=removal.funding,
        action_utxo=_ada_action(BORROW_SIDE),
        exclude=(),
        valid_to_ms=None,
        rng=None,
        deployment=None,
    )
    combined = RemoveAndRedeemSnapshot(collateral=removal, redeem=redeem)
    failed = _script_refused(_redeem_then_remove, combined, (loan,))
    assert failed == ["spend", "withdraw"]
