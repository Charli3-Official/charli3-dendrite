"""Liqwid V2 supply and redeem.

A supply pays ``A`` of a market's underlying into one of its action UTxOs and mints
``q = floor(A / s9)`` qTokens; a redeem burns ``Q`` qTokens and takes ``S = floor(Q *
s9)`` out of an action UTxO. Each spends the action UTxO with the unit redeemer and
re-creates it with the deltas ``[A, q, 0, 0, 0]`` or ``[-S, -Q, 0, 0, 0]``, runs the
qToken policy and references the action and qToken scripts and the market's
MarketParams and MarketState. Both need an upper validity bound at or before the
market's batch deadline ``s8 + p[3]``; neither needs a signature.

The minted qTokens and the redeemed underlying go to the caller's change: the
builders add no wallet output, fee or signer of their own.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.transactions.legs import add_action_leg
from charli3_dendrite.lending.liqwid.transactions.legs import add_funding
from charli3_dendrite.lending.liqwid.transactions.legs import add_owner_signer
from charli3_dendrite.lending.liqwid.transactions.legs import add_qtoken_leg
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
from charli3_dendrite.lending.liqwid.transactions.resolve import record_from_utxo
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.liqwid.transactions.terms import check_action_limits
from charli3_dendrite.lending.liqwid.transactions.terms import check_limits
from charli3_dendrite.lending.liqwid.transactions.terms import check_market_limits
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_burn_for
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_payout
from charli3_dendrite.lending.liqwid.transactions.terms import supply_mint
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.liqwid.transactions.terms import valid_to_ms
from charli3_dendrite.lending.transactions.errors import ValidityWindowError
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.validity import merge_validity
from charli3_dendrite.utility import posix_ms_to_slot
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    import random

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord

__all__ = [
    "RedeemSnapshot",
    "SupplySnapshot",
    "build_redeem",
    "build_supply",
]


# --- snapshots ---------------------------------------------------------------


@dataclass
class SupplySnapshot(PoolActionSnapshot):
    """Everything a supply into one market needs, resolved.

    Attributes:
        market: The market supplied to.
        records: Its MarketParams and MarketState.
        action: The action UTxO the supply spends.
        script_refs: The action validator and qToken policy reference scripts.
        owner_address: The supplier's address (its UTxOs fund the supply).
        amount: The underlying supplied ``A``, in base units.
        qtokens: The qTokens minted, at most ``floor(A / s9)``.
        funding: The wallet UTxOs spent.
        valid_to_ms: The upper validity bound, POSIX ms, at or before the market's
            batch deadline.
        valid_from_ms: An optional lower validity bound, POSIX ms.
        required_signers: Key hashes (hex) required to sign; none by default.
        indefinite: The action datum's style; None writes it definite.
        deployment: The Liqwid deployment; None is the bundled one.
    """

    market: MarketComponents
    records: MarketRecords
    action: ActionUtxo
    script_refs: LiqwidScriptRefs
    owner_address: str
    amount: int
    qtokens: int
    funding: list[Utxo]
    valid_to_ms: int
    valid_from_ms: int | None = None
    required_signers: tuple[str, ...] = ()
    indefinite: bool | None = None
    deployment: LiqwidDeployment | None = None

    @classmethod
    def from_capture(cls, fix: dict[str, Any]) -> SupplySnapshot:
        """Rebuild the snapshot of a captured supply (for byte-exact replay).

        The captured amounts, signers, validity bounds and datum style are pinned.
        """
        flow = _captured_flow(fix)
        return cls(
            market=flow.market,
            records=flow.records,
            action=flow.action,
            script_refs=flow.script_refs,
            owner_address=flow.funding[0].address,
            amount=flow.deltas.supply,
            qtokens=flow.deltas.qtoken,
            funding=flow.funding,
            valid_to_ms=flow.valid_to_ms,
            valid_from_ms=flow.valid_from_ms,
            required_signers=flow.required_signers,
            indefinite=flow.indefinite,
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market: str,
        owner_address: str,
        amount: int,
        action_utxo: str | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
    ) -> SupplySnapshot:
        """Resolve a supply of ``amount`` into the market named ``market``.

        The minimum supply ``p[1]`` is checked before an action UTxO is chosen: the
        pinned ``action_utxo``, or a random one (by ``rng``) not in ``exclude``
        whose share of the supply cap takes the supply. The upper validity bound is
        ``valid_to_ms`` or the default window, capped at the batch deadline. The
        wallet UTxOs at ``owner_address`` fund it unless ``funding`` is given.

        Raises:
            KeyError: No market is named ``market``.
            MinValueError: ``amount`` is below the market minimum.
            CapExceededError: No eligible action UTxO has room under the cap.
            ActionUtxoSpentError: The pinned action UTxO is not live.
            MarketAwaitingBatchError: The market's batch deadline is too close.
        """
        records = _resolve_records(backend, market, deployment)
        rate = records.state_datum.qtoken_rate
        deltas = ActionDeltas(supply=amount, qtoken=supply_mint(amount, rate))
        side = _resolve_action_side(
            backend,
            records,
            flow=Flow.SUPPLY,
            deltas=deltas,
            amount=amount,
            action_utxo=action_utxo,
            exclude=exclude,
            requested_ms=valid_to_ms,
            rng=rng,
            deployment=deployment,
        )
        return cls(
            market=records.market,
            records=records,
            action=side.action,
            script_refs=side.script_refs,
            owner_address=owner_address,
            amount=amount,
            qtokens=deltas.qtoken,
            funding=_funding(backend, owner_address, funding),
            valid_to_ms=side.valid_to_ms,
            deployment=deployment,
        )


@dataclass
class RedeemSnapshot(PoolActionSnapshot):
    """Everything a redeem from one market needs, resolved.

    Attributes:
        market: The market redeemed from.
        records: Its MarketParams and MarketState.
        action: The action UTxO the redeem spends.
        script_refs: The action validator and qToken policy reference scripts.
        owner_address: The redeemer's address (its UTxOs hold the qTokens).
        qtokens: The qTokens burnt ``Q``.
        payout: The underlying paid out ``S``, at most ``floor(Q * s9)``.
        funding: The wallet UTxOs spent; they hold the qTokens burnt.
        valid_to_ms: The upper validity bound, POSIX ms, at or before the market's
            batch deadline.
        valid_from_ms: An optional lower validity bound, POSIX ms.
        required_signers: Key hashes (hex) required to sign; none by default.
        indefinite: The action datum's style; None writes it definite.
        deployment: The Liqwid deployment; None is the bundled one.
    """

    market: MarketComponents
    records: MarketRecords
    action: ActionUtxo
    script_refs: LiqwidScriptRefs
    owner_address: str
    qtokens: int
    payout: int
    funding: list[Utxo]
    valid_to_ms: int
    valid_from_ms: int | None = None
    required_signers: tuple[str, ...] = ()
    indefinite: bool | None = None
    deployment: LiqwidDeployment | None = None

    @classmethod
    def from_capture(cls, fix: dict[str, Any]) -> RedeemSnapshot:
        """Rebuild the snapshot of a captured redeem (for byte-exact replay).

        The captured burn and payout, signers, validity bounds and datum style are
        pinned.
        """
        flow = _captured_flow(fix)
        return cls(
            market=flow.market,
            records=flow.records,
            action=flow.action,
            script_refs=flow.script_refs,
            owner_address=flow.funding[0].address,
            qtokens=-flow.deltas.qtoken,
            payout=-flow.deltas.supply,
            funding=flow.funding,
            valid_to_ms=flow.valid_to_ms,
            valid_from_ms=flow.valid_from_ms,
            required_signers=flow.required_signers,
            indefinite=flow.indefinite,
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market: str,
        owner_address: str,
        amount: int | None = None,
        withdraw_all: bool = False,
        action_utxo: str | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
    ) -> RedeemSnapshot:
        """Resolve a redeem from the market named ``market``.

        Exactly one of ``amount`` and ``withdraw_all``: an ``amount`` of underlying
        burns the fewest qTokens ``Q`` whose payout ``floor(Q * s9)`` covers it;
        ``withdraw_all`` burns every qToken of the market the funding holds. The
        payout is ``floor(Q * s9)``. The wallet UTxOs at ``owner_address`` fund it
        unless ``funding`` is given; they must hold the qTokens burnt.

        The minimum ``p[1]`` is checked on the payout before an action UTxO is
        chosen: the pinned ``action_utxo``, or a random one (by ``rng``) not in
        ``exclude`` that holds the payout. The upper validity bound is
        ``valid_to_ms`` or the default window, capped at the batch deadline.

        Raises:
            ValueError: Neither or both of ``amount`` and ``withdraw_all``; or the
                funding holds fewer qTokens than the redeem burns.
            KeyError: No market is named ``market``.
            MinValueError: The payout is below the market minimum.
            InsufficientLiquidityError: No eligible action UTxO holds the payout.
            ActionUtxoSpentError: The pinned action UTxO is not live.
            MarketAwaitingBatchError: The market's batch deadline is too close.
        """
        if (amount is None) == (not withdraw_all):
            msg = "a redeem takes exactly one of an amount and withdraw_all"
            raise ValueError(msg)
        records = _resolve_records(backend, market, deployment)
        wallet = _funding(backend, owner_address, funding)
        held = _qtokens_held(wallet, records.market)
        rate = records.state_datum.qtoken_rate
        qtokens = held if amount is None else redeem_burn_for(amount, rate)
        if qtokens <= 0:
            msg = f"the funding holds no qTokens of market {records.market.name}"
            raise ValueError(msg)
        if qtokens > held:
            msg = (
                f"the redeem burns {qtokens} qTokens of market {records.market.name}"
                f" and the funding holds {held}"
            )
            raise ValueError(msg)
        payout = redeem_payout(qtokens, rate)
        side = _resolve_action_side(
            backend,
            records,
            flow=Flow.REDEEM,
            deltas=ActionDeltas(supply=-payout, qtoken=-qtokens),
            amount=payout,
            action_utxo=action_utxo,
            exclude=exclude,
            requested_ms=valid_to_ms,
            rng=rng,
            deployment=deployment,
        )
        return cls(
            market=records.market,
            records=records,
            action=side.action,
            script_refs=side.script_refs,
            owner_address=owner_address,
            qtokens=qtokens,
            payout=payout,
            funding=wallet,
            valid_to_ms=side.valid_to_ms,
            deployment=deployment,
        )


# --- builds ------------------------------------------------------------------


def build_supply(tx_builder: TransactionBuilder, *, snapshot: SupplySnapshot) -> None:
    """Add the supply of ``snapshot`` to ``tx_builder``.

    The caller balances (the minted qTokens land in its change), signs and
    submits. Legs already on the builder are kept; the validity window is
    intersected with the one already set.

    Raises:
        ValueError: The snapshot mints more than ``floor(A / s9)`` qTokens.
        MinValueError: The amount is below the market minimum.
        CapExceededError: The supply passes the action UTxO's supply cap.
        ValidityWindowError: The upper bound is past the batch deadline or the
            merged window is empty.
    """
    _build_supply(tx_builder, snapshot, stash=snapshot)


def build_redeem(tx_builder: TransactionBuilder, *, snapshot: RedeemSnapshot) -> None:
    """Add the redeem of ``snapshot`` to ``tx_builder``.

    The caller balances (the payout lands in its change), signs and submits. Legs
    already on the builder are kept; the validity window is intersected with the
    one already set.

    Raises:
        ValueError: The snapshot pays more than ``floor(Q * s9)``.
        MinValueError: The payout is below the market minimum.
        InsufficientLiquidityError: The action UTxO cannot pay the payout.
        ValidityWindowError: The upper bound is past the batch deadline or the
            merged window is empty.
    """
    _build_redeem(tx_builder, snapshot, stash=snapshot)


def _build_supply(
    tx_builder: TransactionBuilder,
    snapshot: SupplySnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_supply`, stashing the funding's Ogmios entries on ``stash``."""
    rate = snapshot.records.state_datum.qtoken_rate
    most = supply_mint(snapshot.amount, rate)
    if snapshot.qtokens > most:
        msg = (
            f"a supply of {snapshot.amount} mints at most {most} qTokens, "
            f"not {snapshot.qtokens}"
        )
        raise ValueError(msg)
    _add_action_flow(
        tx_builder,
        snapshot,
        flow=Flow.SUPPLY,
        deltas=ActionDeltas(supply=snapshot.amount, qtoken=snapshot.qtokens),
        amount=snapshot.amount,
        stash=stash,
    )


def _build_redeem(
    tx_builder: TransactionBuilder,
    snapshot: RedeemSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_redeem`, stashing the funding's Ogmios entries on ``stash``."""
    rate = snapshot.records.state_datum.qtoken_rate
    most = redeem_payout(snapshot.qtokens, rate)
    if snapshot.payout > most:
        msg = (
            f"a burn of {snapshot.qtokens} qTokens pays at most {most}, "
            f"not {snapshot.payout}"
        )
        raise ValueError(msg)
    _add_action_flow(
        tx_builder,
        snapshot,
        flow=Flow.REDEEM,
        deltas=ActionDeltas(supply=-snapshot.payout, qtoken=-snapshot.qtokens),
        amount=snapshot.payout,
        stash=stash,
    )


def _add_action_flow(
    tx_builder: TransactionBuilder,
    snapshot: SupplySnapshot | RedeemSnapshot,
    *,
    flow: Flow,
    deltas: ActionDeltas,
    amount: int,
    stash: PoolActionSnapshot,
) -> None:
    """The legs a supply or redeem adds, after its limits are checked.

    Funding, the action leg with ``deltas`` (referencing the MarketParams and
    MarketState), the qToken mint or burn of ``deltas.qtoken``, the snapshot's
    signers and its validity window. The limits and the upper bound are checked
    before anything is added.
    """
    records = snapshot.records
    params, state = records.params_datum, records.state_datum
    underlying = snapshot.market.underlying
    check_limits(
        flow,
        params=params,
        state=state,
        action_datum=snapshot.action.datum,
        deltas=deltas,
        amount=amount,
        output_collateral_count=0,
        action_underlying=snapshot.action.underlying_amount(underlying),
        underlying=underlying,
    )
    upper, lower = _validity_slots(snapshot, records)
    add_funding(tx_builder, stash, snapshot.funding)
    add_action_leg(
        tx_builder,
        action=snapshot.action,
        deltas=deltas,
        script_ref=snapshot.script_refs.action,
        references=[
            utxo_from_info(records.params.info),
            utxo_from_info(records.state.info),
        ],
        underlying=underlying,
        indefinite=snapshot.indefinite,
    )
    add_qtoken_leg(
        tx_builder,
        policy_ref=snapshot.script_refs.qtoken_policy,
        qtoken_policy=snapshot.market.qtoken_policy,
        quantity=deltas.qtoken,
    )
    for signer in snapshot.required_signers:
        add_owner_signer(tx_builder, signer)
    merge_validity(tx_builder, upper_slot=upper, lower_slot=lower)


def _validity_slots(
    snapshot: SupplySnapshot | RedeemSnapshot,
    records: MarketRecords,
) -> tuple[int, int | None]:
    """The snapshot's validity bounds as slots, the upper one checked.

    Raises:
        ValidityWindowError: The upper bound is past the batch deadline.
    """
    cap = ttl_cap_ms(records.state_datum, records.params_datum)
    if snapshot.valid_to_ms > cap:
        msg = (
            f"upper bound {snapshot.valid_to_ms} ms is past market "
            f"{records.market.name}'s batch deadline {cap} ms"
        )
        raise ValidityWindowError(msg)
    lower = snapshot.valid_from_ms
    return (
        posix_ms_to_slot(snapshot.valid_to_ms),
        None if lower is None else posix_ms_to_slot(lower),
    )


# --- live resolution ---------------------------------------------------------


def _resolve_records(
    backend: AbstractBackend,
    name: str,
    deployment: LiqwidDeployment | None,
) -> MarketRecords:
    """The live records of the market named ``name``.

    Raises:
        KeyError: No market is named ``name``.
    """
    from charli3_dendrite.lending.liqwid.transactions.resolve import (
        resolve_market_records,
    )

    market = resolve_deployment(deployment).market_by_name(name)
    return resolve_market_records(backend, [market], deployment=deployment)[name]


@dataclass(frozen=True)
class _ActionSide:
    """The chosen action UTxO, the scripts and the upper bound of an action flow."""

    action: ActionUtxo
    script_refs: LiqwidScriptRefs
    valid_to_ms: int


def _resolve_action_side(
    backend: AbstractBackend,
    records: MarketRecords,
    *,
    flow: Flow,
    deltas: ActionDeltas,
    amount: int,
    action_utxo: str | None,
    exclude: Collection[str],
    requested_ms: int | None,
    rng: random.Random | None,
    deployment: LiqwidDeployment | None,
) -> _ActionSide:
    """Check the market limits, then choose an action UTxO that takes ``deltas``.

    The market-level limits run first, then each candidate action UTxO's limits
    (`check_action_limits`) decide which may be chosen. The action and qToken
    reference scripts are resolved and the upper bound is ``requested_ms`` or the
    default window from the chain tip (`valid_to_ms`).
    """
    from charli3_dendrite.lending.liqwid.transactions.resolve import choose_action_utxo
    from charli3_dendrite.lending.liqwid.transactions.resolve import (
        resolve_action_utxos,
    )
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_now_ms
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_script_refs

    market = records.market
    params, state = records.params_datum, records.state_datum
    check_market_limits(flow, params=params, amount=amount, output_collateral_count=0)

    def check(candidate: ActionUtxo) -> None:
        check_action_limits(
            flow,
            params=params,
            state=state,
            action_datum=candidate.datum,
            deltas=deltas,
            action_underlying=candidate.underlying_amount(market.underlying),
            underlying=market.underlying,
        )

    action = choose_action_utxo(
        resolve_action_utxos(backend, market, params),
        check=check,
        pin=action_utxo,
        exclude=exclude,
        rng=rng,
    )
    upper = valid_to_ms(
        resolve_now_ms(backend),
        requested_ms,
        state=state,
        params=params,
        market=market.name,
    )
    script_refs = resolve_script_refs(
        backend,
        market,
        params,
        deployment=deployment,
        loans=False,
    )
    return _ActionSide(action=action, script_refs=script_refs, valid_to_ms=upper)


def _funding(
    backend: AbstractBackend,
    owner_address: str,
    funding: Sequence[Utxo] | None,
) -> list[Utxo]:
    if funding is not None:
        return list(funding)
    from charli3_dendrite.lending.transactions.resolve import resolve_funding

    return resolve_funding(backend, owner_address)


def _qtokens_held(funding: Sequence[Utxo], market: MarketComponents) -> int:
    """The qTokens of ``market`` (empty asset name) ``funding`` holds."""
    return sum(
        qty
        for u in funding
        for policy, name, qty in u.assets
        if policy == market.qtoken_policy and name == ""
    )


# --- captures ----------------------------------------------------------------


@dataclass(frozen=True)
class _CapturedFlow:
    """The parts of a captured supply or redeem its snapshot pins."""

    market: MarketComponents
    records: MarketRecords
    action: ActionUtxo
    script_refs: LiqwidScriptRefs
    deltas: ActionDeltas
    funding: list[Utxo]
    valid_to_ms: int
    valid_from_ms: int | None
    required_signers: tuple[str, ...]
    indefinite: bool


def _captured_flow(fix: dict[str, Any]) -> _CapturedFlow:
    """Read a captured supply or redeem of one market.

    The market is the one whose qTokens the capture mints or burns; its MarketParams
    and MarketState are the referenced records, its action UTxO the spent input
    holding its action token, and the deltas the change of that UTxO's record. Every
    other input is funding.

    Raises:
        ValueError: The capture does not mint or burn exactly one market's qTokens,
            or lacks the market's records, action UTxO or scripts.
    """
    active = resolve_deployment()
    markets = {
        found
        for policy, _, _ in fix["mints"]
        if (found := active.market_of_qtoken(policy)) is not None
    }
    if len(markets) != 1:
        msg = f"capture {fix['label']} does not mint or burn one market's qTokens"
        raise ValueError(msg)
    (market,) = markets
    inputs = [utxo_from_dict(u) for u in fix["inputs"]]
    refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
    records = _captured_records(refs, market)
    spent = _holder(inputs, market, fix)
    written = _holder([utxo_from_dict(u) for u in fix["outputs"]], market, fix)
    before, after = decode_action(spent.datum or ""), decode_action(written.datum or "")
    params = records.params_datum
    if params.action_validator is None:
        msg = f"market {market.name} MarketParams names no action validator"
        raise ValueError(msg)
    lower = fix["invalid_before"]
    return _CapturedFlow(
        market=market,
        records=records,
        action=ActionUtxo(utxo=spent, datum=before),
        script_refs=LiqwidScriptRefs(
            action=script_ref_by_hash(refs, params.action_validator),
            qtoken_policy=script_ref_by_hash(refs, market.qtoken_policy),
            loan_validator=None,
            loan_policy=None,
            borrow_staking=None,
            liquidation_staking=None,
        ),
        deltas=ActionDeltas(
            supply=after.supply_delta - before.supply_delta,
            qtoken=after.qtoken_delta - before.qtoken_delta,
        ),
        funding=[u for u in inputs if u.out_ref != spent.out_ref],
        valid_to_ms=slot_to_posix_ms(fix["invalid_hereafter"]),
        valid_from_ms=None if lower is None else slot_to_posix_ms(lower),
        required_signers=tuple(fix["required_signers"]),
        indefinite=datum_style(written.datum or "") == "indefinite",
    )


def _captured_records(refs: Sequence[Utxo], market: MarketComponents) -> MarketRecords:
    found: dict[LiqwidKind, LiqwidRecord] = {}
    for u in refs:
        record = record_from_utxo(u)
        if record is not None and record.market_id == market.market_id:
            found.setdefault(record.kind, record)
    try:
        params = found[LiqwidKind.MARKET_PARAMS]
        state = found[LiqwidKind.MARKET_STATE]
    except KeyError as missing:
        msg = f"no referenced {missing.args[0].value} record of market {market.name}"
        raise ValueError(msg) from None
    return MarketRecords(
        market=market,
        params=params,
        state=state,
        collateral_params={},
    )


def _holder(utxos: Sequence[Utxo], market: MarketComponents, fix: dict) -> Utxo:
    """The one UTxO of ``utxos`` holding ``market``'s action token."""
    policy = market.action_token_policy
    holders = [u for u in utxos if policy is not None and u.holds_policy(policy)]
    if len(holders) != 1:
        msg = (
            f"capture {fix['label']} has {len(holders)} action UTxOs of market "
            f"{market.name} on one side"
        )
        raise ValueError(msg)
    return holders[0]
