"""Liqwid V2 repay: a partial repay, a close, and the close of a zero-debt loan.

A repay of ``R`` on a loan settled to the market index (``Ds`` its settled debt and
``p0 + max(iS, m0)`` its payoff, `terms.close_payment`) takes one of three paths,
each ending in the shared end of a loan flow (the owner's signature and the zero
withdrawal from the borrow staking script):

- **Partial repay** (``R <= Ds``): the loan is settled and repaid interest first
  (`terms.apply_repay`), its token flips, and it is re-created with the same or a
  target collateral. ``R`` is paid into an action UTxO. The health set of the
  output collateral is referenced; health is checked only when collateral
  decreases.
- **Close** (``R >= payoff``): exactly the payoff is paid into an action UTxO with
  `terms.close_deltas`, the loan token is burnt and no loan output remains; the
  collateral and the loan lovelace go to the caller's change. A loan owing only its
  minimum interest ``(0, 0, m0 > 0)`` closes this way, paying ``m0``.
- **Zero-debt close** (a ``(0, 0, 0)`` loan): the loan is spent and its token burnt,
  with no action UTxO; it needs no validity window.

A repay strictly between ``Ds`` and the payoff is refused (`RepayAmountError`): the
interest-first rule would write a negative principal.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.transactions._flow import check_flow_limits
from charli3_dendrite.lending.liqwid.transactions._flow import kept_loan_lovelace
from charli3_dendrite.lending.liqwid.transactions._flow import loan_target
from charli3_dendrite.lending.liqwid.transactions._flow import market_references
from charli3_dendrite.lending.liqwid.transactions._flow import merge_validity_ms
from charli3_dendrite.lending.liqwid.transactions._flow import required_script
from charli3_dendrite.lending.liqwid.transactions._flow import written_loan
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.capture import sole
from charli3_dendrite.lending.liqwid.transactions.legs import add_action_leg
from charli3_dendrite.lending.liqwid.transactions.legs import add_funding
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_input
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_token_change
from charli3_dendrite.lending.liqwid.transactions.legs import finish_loan_flow
from charli3_dendrite.lending.liqwid.transactions.legs import flip_name
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.liqwid.transactions.terms import check_market_limits
from charli3_dendrite.lending.liqwid.transactions.terms import close_deltas
from charli3_dendrite.lending.liqwid.transactions.terms import close_payment
from charli3_dendrite.lending.liqwid.transactions.terms import health_debt
from charli3_dendrite.lending.liqwid.transactions.terms import held_collateral
from charli3_dendrite.lending.liqwid.transactions.terms import repay_from_settlement
from charli3_dendrite.lending.liqwid.transactions.terms import settle
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.common import merge_reference_inputs
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.transactions.utxos import Utxo

if TYPE_CHECKING:
    import random

    from pycardano import TransactionBuilder

    from charli3_dendrite.backend.backend_base import AbstractBackend

__all__ = ["RepaySnapshot", "build_repay"]

_T = TypeVar("_T")


# --- the repay plan ----------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    """What a repay of a loan writes.

    ``deltas`` is None for a zero-debt close, which spends no action UTxO;
    ``loan`` and ``collateral`` (the output loan's collateral) are None on a close.
    ``payment`` is what the action UTxO receives and ``debt`` what the health check
    measures.
    """

    flow: Flow
    payment: int
    debt: int
    deltas: ActionDeltas | None
    loan: LoanDatum | None
    collateral: dict[str, int] | None


def _owes_nothing(loan: LoanDatum) -> bool:
    return loan.principal == loan.interest == loan.min_interest == 0


def _plan(
    loan: LoanRecord,
    s5: int,
    repay: int,
    collateral: Mapping[str, int] | None,
) -> _Plan:
    """The path a repay of ``repay`` takes on ``loan`` at the market index ``s5``.

    Raises:
        ValueError: ``repay`` is negative, or a close is given a collateral target.
        RepayAmountError: ``repay`` is above the settled debt and below the payoff.
    """
    if repay < 0:
        msg = f"negative repay {repay}"
        raise ValueError(msg)
    datum = loan.datum
    payoff = close_payment(datum, s5)
    if repay >= payoff:
        if collateral is not None:
            msg = "a close returns the whole collateral and takes no collateral target"
            raise ValueError(msg)
        if _owes_nothing(datum):
            return _Plan(Flow.CLOSE, 0, 0, None, None, None)
        return _Plan(Flow.CLOSE, payoff, 0, close_deltas(datum, s5), None, None)
    settlement = settle(datum, s5)
    result = repay_from_settlement(datum, settlement, repay)
    target = loan.collateral if collateral is None else dict(collateral)
    debt = health_debt(
        datum.principal,
        settlement.interest,
        datum.min_interest,
        0,
        repay,
    )
    return _Plan(Flow.REPAY, repay, debt, result.deltas, result.loan, target)


def _required(value: _T | None, what: str) -> _T:
    """``value``; refused with ``ValueError`` naming ``what`` when it is None."""
    if value is None:
        msg = f"this repay needs {what}"
        raise ValueError(msg)
    return value


# --- snapshot ----------------------------------------------------------------


@dataclass
class RepaySnapshot(PoolActionSnapshot):
    """Everything a repay of one Liqwid V2 loan needs, resolved.

    ``repay`` is the payment ``R`` (`closes` when it reaches the payoff, which is
    then paid exactly). ``action`` is None on a zero-debt close and ``health`` None
    on any close. ``collateral`` is a partial repay's target collateral (None keeps
    the loan's). ``valid_to_ms`` bounds an action-spending repay; a zero-debt close
    needs none, and one it is given is written as well. Either is capped at the
    market's batch deadline; ``valid_from_ms`` is written whenever it is set.
    ``loan_lovelace`` is the least lovelace the output loan keeps (the input loan's
    by default), ``indefinite`` the array style of the datums written (definite when
    None) and ``deployment`` the Liqwid deployment (the bundled one when None).
    """

    loan: LoanRecord
    market: MarketComponents
    records: MarketRecords
    action: ActionUtxo | None
    script_refs: LiqwidScriptRefs
    health: HealthInputs | None
    owner_address: str
    repay: int
    collateral: dict[str, int] | None
    funding: list[Utxo]
    valid_to_ms: int | None
    valid_from_ms: int | None = None
    loan_lovelace: int | None = None
    deployment: LiqwidDeployment | None = None
    indefinite: bool | None = None

    @property
    def payoff(self) -> int:
        """The payment that closes the loan, ``p0 + max(iS, m0)``."""
        return close_payment(self.loan.datum, self.records.state_datum.index)

    @property
    def closes(self) -> bool:
        """True when the repay reaches the payoff and closes the loan."""
        return self.repay >= self.payoff

    @classmethod
    def from_capture(cls, fix: Mapping[str, Any]) -> RepaySnapshot:
        """Rebuild the snapshot of a captured repay or close, for byte-exact replay.

        The captured loan lovelace, datum style and validity window are pinned, and
        ``repay`` is what the captured action UTxO received.

        Raises:
            ValueError: The capture spends no loan or several.
            UnsupportedMarketError: The capture references no MarketParams or
                MarketState of the loan's market.
        """
        capture = CapturedTx.of(fix)
        loan: LoanRecord = sole(capture.loan_inputs(), "loan input")
        market = loan.market
        borrow = capture.market_records(market)
        action_in = next(iter(capture.action_inputs(market)), None)
        action_out = next(iter(capture.action_outputs(market)), None)
        loan_out = next(iter(capture.loan_outputs()), None)
        collateral = loan_out.collateral if loan_out is not None else None
        repay = (
            action_out.underlying_amount(market.underlying)
            - action_in.underlying_amount(market.underlying)
            if action_in is not None and action_out is not None
            else 0
        )
        spent = [loan.utxo] + ([action_in.utxo] if action_in is not None else [])
        written = [u.utxo for u in (loan_out, action_out) if u is not None]
        return cls(
            loan=loan,
            market=market,
            records=borrow,
            action=action_in,
            script_refs=capture.script_refs(market, borrow.params_datum, require=()),
            health=(
                None
                if collateral is None
                else capture.health(held_collateral(collateral), borrow=borrow)
            ),
            owner_address=capture.wallet_address(*spent, *written),
            repay=repay,
            collateral=(
                collateral
                if collateral is not None and collateral != loan.collateral
                else None
            ),
            funding=capture.funding(*spent),
            valid_to_ms=capture.valid_to_ms,
            valid_from_ms=capture.valid_from_ms,
            loan_lovelace=loan_out.utxo.lovelace if loan_out is not None else None,
            indefinite=capture.indefinite(*written),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: str,
        owner_address: str,
        repay: int,
        collateral: Mapping[str, int] | None = None,
        action_utxo: str | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
        allow_spent: bool = False,
    ) -> RepaySnapshot:
        """Resolve a repay of ``repay`` on the loan at ``loan_utxo`` from live state.

        The loan and its market's records are resolved first and the repay planned
        against the market index, so a repay strictly between the settled debt and
        the payoff is refused before anything else is read. Then the market-level
        limits, the reference scripts the path runs, the action UTxO (``action_utxo``
        pins one; otherwise a random choice by ``rng`` among those not in
        ``exclude`` that pass the action limits), the health inputs of a partial
        repay's output collateral, the validity bound (``valid_to_ms``, or the
        default, of an action-spending repay) and the funding (``funding``, or the
        UTxOs at ``owner_address``).

        Raises:
            RepayAmountError: ``repay`` is above the settled debt and below the
                payoff.
            ValueError: ``repay`` is negative, a close is given a collateral
                target, or a target quantity is negative (before anything is
                read).
            LiqwidBuildError: a limit, an action UTxO, a record or a feed the
                repay needs is refused or missing.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_flow_action,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_health_inputs,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_owner_funding,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_script_refs,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_valid_to_ms,
        )

        if collateral is not None:
            held_collateral(collateral)
        loan = resolve_loan(
            backend,
            loan_utxo,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        market = loan.market
        records = resolve_market(backend, market, deployment=deployment)
        params = records.params_datum
        plan = _plan(loan, records.state_datum.index, repay, collateral)
        held = held_collateral(plan.collateral or {})
        check_market_limits(
            plan.flow,
            params=params,
            amount=plan.payment,
            output_collateral_count=len(held),
        )
        script_refs = resolve_script_refs(
            backend,
            market,
            params,
            deployment=deployment,
        )
        action: ActionUtxo | None = None
        bound: int | None = None
        if plan.deltas is not None:
            action = resolve_flow_action(
                backend,
                records,
                flow=plan.flow,
                deltas=plan.deltas,
                pin=action_utxo,
                exclude=exclude,
                rng=rng,
            )
            bound = resolve_valid_to_ms(backend, records, valid_to_ms)
        health = (
            None
            if plan.collateral is None
            else resolve_health_inputs(
                backend,
                held,
                borrow=records,
                deployment=deployment,
            )
        )
        return cls(
            loan=loan,
            market=market,
            records=records,
            action=action,
            script_refs=script_refs,
            health=health,
            owner_address=owner_address,
            repay=repay,
            collateral=None if collateral is None else dict(collateral),
            funding=resolve_owner_funding(backend, owner_address, funding),
            valid_to_ms=bound,
            deployment=deployment,
        )


# --- build -------------------------------------------------------------------


def build_repay(tx_builder: TransactionBuilder, *, snapshot: RepaySnapshot) -> None:
    """Add a repay of ``snapshot`` to ``tx_builder`` (see the module docstring).

    The market's loan scripts and every reference script the repay runs are
    checked before anything is added; the health check and the reference hygiene
    run after the legs are added, so discard the builder when they raise.

    Raises:
        RepayAmountError: the repay is above the settled debt and below the payoff.
        MinValueError: a partial repay is below the market minimum ``p[1]``.
        CollateralCountError: the output loan holds too many collateral assets.
        UnhealthyLoanError: a collateral decrease leaves the loan unhealthy.
        ForeignReferenceInputError: the transaction references an input the loan
            validator would reject.
        UnsupportedMarketError: the market's MarketParams name no loan validator,
            it has no current loan-token policy, or a script the repay runs is
            missing from ``snapshot.script_refs``.
        ValueError: the snapshot lacks what its path needs (an action UTxO, a
            validity bound), or a close is given a collateral target.
    """
    _build_repay(tx_builder, snapshot, stash=snapshot)


def _build_repay(
    tx_builder: TransactionBuilder,
    snapshot: RepaySnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_repay`, stashing the funding on ``stash``."""
    records = snapshot.records
    params, state = records.params_datum, records.state_datum
    market = snapshot.market
    loan = snapshot.loan
    refs = snapshot.script_refs
    plan = _plan(loan, state.index, snapshot.repay, snapshot.collateral)
    target = loan_target(records)
    loan_validator = required_script(refs.loan_validator, market, "loan validator")
    loan_policy = required_script(refs.loan_policy, market, "loan-token policy")
    required_script(refs.borrow_staking, market, "borrow staking")
    market_refs = market_references(records)
    action: ActionUtxo | None = None
    action_script: Utxo | None = None
    cap = ttl_cap_ms(state, params)
    # A zero-debt close needs no window; a bound it is given is capped as well.
    upper = None if snapshot.valid_to_ms is None else min(snapshot.valid_to_ms, cap)
    if plan.deltas is not None:
        action = _required(snapshot.action, "an action UTxO")
        action_script = required_script(refs.action, market, "action validator")
        check_flow_limits(
            plan.flow,
            records=records,
            action=action,
            deltas=plan.deltas,
            amount=plan.payment,
            output_collateral_count=len(held_collateral(plan.collateral or {})),
        )
        upper = min(_required(snapshot.valid_to_ms, "an upper validity bound"), cap)
    elif snapshot.action is not None:
        msg = "a zero-debt close spends no action UTxO"
        raise ValueError(msg)
    new_name = flip_name(loan.token_name) if plan.loan is not None else None
    add_funding(tx_builder, stash, snapshot.funding)
    add_loan_input(tx_builder, loan=loan.utxo, script_ref=loan_validator)
    add_loan_token_change(
        tx_builder,
        policy_ref=loan_policy,
        loan_policy=target.policy,
        current_name=loan.token_name,
        new_name=new_name,
    )
    if action is not None and action_script is not None and plan.deltas is not None:
        add_action_leg(
            tx_builder,
            action=action,
            deltas=plan.deltas,
            script_ref=action_script,
            references=market_refs,
            underlying=market.underlying,
            indefinite=snapshot.indefinite,
        )
    else:
        # A zero-debt close runs the loan validator, the loan-token policy and the
        # borrow staking script, which read the market's MarketParams and
        # MarketState: no action UTxO, no health set and no feed.
        merge_reference_inputs(tx_builder, market_refs)
    if plan.loan is not None and new_name is not None:
        tx_builder.add_output(
            written_loan(
                target,
                datum=plan.loan,
                indefinite=snapshot.indefinite,
                loan_token=new_name,
                collateral=plan.collateral or {},
                previous_lovelace=kept_loan_lovelace(loan, snapshot.loan_lovelace),
            ),
        )
    finish_loan_flow(
        tx_builder,
        records=records,
        script_refs=refs,
        health=snapshot.health,
        owner=loan.datum.owner,
        collateral_in=loan.collateral,
        collateral_out=plan.collateral,
        debt=plan.debt,
        borrow=0,
        repay=plan.payment,
        deployment=resolve_deployment(snapshot.deployment),
    )
    merge_validity_ms(tx_builder, upper_ms=upper, lower_ms=snapshot.valid_from_ms)
