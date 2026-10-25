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
  with no action UTxO and no validity window.

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
from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.datums import encode_loan_datum
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.deployment import staking_roles
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import action_selector
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.liqwid.transactions import terms
from charli3_dendrite.lending.liqwid.transactions.legs import add_action_leg
from charli3_dendrite.lending.liqwid.transactions.legs import add_funding
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_input
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_token_change
from charli3_dendrite.lending.liqwid.transactions.legs import finish_loan_flow
from charli3_dendrite.lending.liqwid.transactions.legs import flip_name
from charli3_dendrite.lending.liqwid.transactions.legs import loan_output
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
from charli3_dendrite.lending.liqwid.transactions.resolve import action_record
from charli3_dendrite.lending.liqwid.transactions.resolve import health_inputs
from charli3_dendrite.lending.liqwid.transactions.resolve import loan_record
from charli3_dendrite.lending.liqwid.transactions.resolve import market_records
from charli3_dendrite.lending.liqwid.transactions.resolve import record_from_utxo
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.liqwid.transactions.terms import check_action_limits
from charli3_dendrite.lending.liqwid.transactions.terms import check_limits
from charli3_dendrite.lending.liqwid.transactions.terms import check_market_limits
from charli3_dendrite.lending.liqwid.transactions.terms import close_deltas
from charli3_dendrite.lending.liqwid.transactions.terms import close_payment
from charli3_dendrite.lending.liqwid.transactions.terms import health_debt
from charli3_dendrite.lending.liqwid.transactions.terms import repay_from_settlement
from charli3_dendrite.lending.liqwid.transactions.terms import settle
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.common import merge_reference_inputs
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.lending.transactions.validity import merge_validity
from charli3_dendrite.lending.units import script_payment_address
from charli3_dendrite.utility import posix_ms_to_slot
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    import random

    from pycardano import TransactionBuilder

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.indexing import LiqwidSelector

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


def _held(collateral: Mapping[str, int] | None) -> list[str]:
    return [unit for unit, qty in (collateral or {}).items() if qty > 0]


def _required(value: _T | None, what: str) -> _T:
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
    the loan's). ``valid_to_ms`` bounds an action-spending repay (capped at the
    market's batch deadline); a zero-debt close needs none. ``loan_lovelace`` is the
    least lovelace the output loan keeps (the input loan's by default), ``indefinite``
    the array style of the datums written (definite when None) and ``deployment``
    the Liqwid deployment (the bundled one when None).
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
        """
        inputs = [utxo_from_dict(u) for u in fix["inputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        outputs = [utxo_from_dict(u) for u in fix["outputs"]]
        loan = next(
            loan_record(u)
            for u in inputs
            if (r := record_from_utxo(u)) is not None and r.kind is LiqwidKind.LOAN
        )
        market = loan.market
        records = [r for u in refs if (r := record_from_utxo(u)) is not None]
        borrow = market_records(records, market)
        params = borrow.params_datum
        selector = action_selector(market, params)
        action_in = _action(inputs, market, selector)
        action_out = _action(outputs, market, selector)
        loan_out = next(
            (u for u in outputs if u.holds_policy(market.new_borrow_policy or "")),
            None,
        )
        out_record = loan_record(loan_out) if loan_out is not None else None
        collateral = out_record.collateral if out_record is not None else None
        repay = (
            action_out.underlying_amount(market.underlying)
            - action_in.underlying_amount(market.underlying)
            if action_in is not None and action_out is not None
            else 0
        )
        roles = staking_roles(market, params)
        script_refs = LiqwidScriptRefs(
            action=_script(refs, params.action_validator),
            qtoken_policy=_script(refs, market.qtoken_policy),
            loan_validator=_script(refs, params.loan_validator),
            loan_policy=_script(refs, market.new_borrow_policy),
            borrow_staking=_script(refs, roles.borrow),
            liquidation_staking=_script(refs, roles.liquidation),
        )
        spent = {loan.utxo.out_ref, action_in.utxo.out_ref if action_in else None}
        funding = [u for u in inputs if u.out_ref not in spent]
        styles = {
            datum_style(u.datum)
            for u in (loan_out, action_out.utxo if action_out else None)
            if u is not None and u.datum
        }
        return cls(
            loan=loan,
            market=market,
            records=borrow,
            action=action_in,
            script_refs=script_refs,
            health=(
                None
                if collateral is None
                else health_inputs(
                    _held(collateral),
                    borrow=borrow,
                    records=records,
                    feeds=[
                        feed
                        for u in refs
                        if (feed := parse_feed(utxo_to_info(u))) is not None
                    ],
                )
            ),
            owner_address=funding[0].address,
            repay=repay,
            collateral=(
                collateral
                if collateral is not None and collateral != loan.collateral
                else None
            ),
            funding=funding,
            valid_to_ms=_slot_ms(fix["invalid_hereafter"]),
            valid_from_ms=_slot_ms(fix["invalid_before"]),
            loan_lovelace=loan_out.lovelace if loan_out is not None else None,
            indefinite=True if "indefinite" in styles else None,
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
            ValueError: ``repay`` is negative, or a close is given a collateral
                target.
            LiqwidBuildError: a limit, an action UTxO, a record or a feed the
                repay needs is refused or missing.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            choose_action_utxo,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_action_utxos,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_health_inputs,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_market_records,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_now_ms
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_script_refs,
        )
        from charli3_dendrite.lending.transactions.resolve import resolve_funding

        loan = resolve_loan(
            backend,
            loan_utxo,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        market = loan.market
        records = resolve_market_records(backend, [market], deployment=deployment)[
            market.name
        ]
        params, state = records.params_datum, records.state_datum
        plan = _plan(loan, state.index, repay, collateral)
        held = _held(plan.collateral)
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
            deltas = plan.deltas

            def check(candidate: ActionUtxo) -> None:
                check_action_limits(
                    plan.flow,
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
            bound = terms.valid_to_ms(
                resolve_now_ms(backend),
                valid_to_ms,
                state=state,
                params=params,
                market=market.name,
            )
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
            funding=(
                list(funding)
                if funding is not None
                else resolve_funding(backend, owner_address)
            ),
            valid_to_ms=bound,
            deployment=deployment,
        )


# --- capture decoding --------------------------------------------------------


def _slot_ms(slot: int | None) -> int | None:
    return None if slot is None else slot_to_posix_ms(slot)


def _action(
    utxos: Sequence[Utxo],
    market: MarketComponents,
    selector: LiqwidSelector,
) -> ActionUtxo | None:
    """The first action UTxO of ``market`` among ``utxos`` (:func:`action_record`)."""
    return next(
        (
            action
            for u in utxos
            if (action := action_record(utxo_to_info(u), market, selector)) is not None
        ),
        None,
    )


def _script(refs: Sequence[Utxo], script_hash: str | None) -> Utxo | None:
    """The reference input carrying the script ``script_hash``, if any."""
    return next(
        (u for u in refs if u.ref_script and script_hash_of(u) == script_hash),
        None,
    )


# --- build -------------------------------------------------------------------


def build_repay(tx_builder: TransactionBuilder, *, snapshot: RepaySnapshot) -> None:
    """Add a repay of ``snapshot`` to ``tx_builder`` (see the module docstring).

    Raises:
        RepayAmountError: the repay is above the settled debt and below the payoff.
        MinValueError: a partial repay is below the market minimum ``p[1]``.
        CollateralCountError: the output loan holds too many collateral assets.
        UnhealthyLoanError: a collateral decrease leaves the loan unhealthy.
        ForeignReferenceInputError: the transaction references an input the loan
            validator would reject.
        ValueError: the snapshot lacks what its path needs (an action UTxO, a
            validity bound, a reference script), or a close is given a
            collateral target.
    """
    _build_repay(tx_builder, snapshot, stash=snapshot)


def _build_repay(
    tx_builder: TransactionBuilder,
    snapshot: RepaySnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    records = snapshot.records
    params, state = records.params_datum, records.state_datum
    market = snapshot.market
    loan = snapshot.loan
    refs = snapshot.script_refs
    plan = _plan(loan, state.index, snapshot.repay, snapshot.collateral)
    loan_policy: str = _required(
        market.new_borrow_policy,
        "a current loan-token policy",
    )
    market_refs = [
        utxo_from_info(records.params.info),
        utxo_from_info(records.state.info),
    ]
    action: ActionUtxo | None = None
    upper = snapshot.valid_to_ms
    if plan.deltas is not None:
        action = _required(snapshot.action, "an action UTxO")
        check_limits(
            plan.flow,
            params=params,
            state=state,
            action_datum=action.datum,
            deltas=plan.deltas,
            amount=plan.payment,
            output_collateral_count=len(_held(plan.collateral)),
            action_underlying=action.underlying_amount(market.underlying),
            underlying=market.underlying,
        )
        upper = min(
            _required(snapshot.valid_to_ms, "an upper validity bound"),
            ttl_cap_ms(state, params),
        )
    elif snapshot.action is not None:
        msg = "a zero-debt close spends no action UTxO"
        raise ValueError(msg)
    new_name = flip_name(loan.token_name) if plan.loan is not None else None
    add_funding(tx_builder, stash, snapshot.funding)
    add_loan_input(
        tx_builder,
        loan=loan.utxo,
        script_ref=_required(refs.loan_validator, "the loan validator script"),
    )
    add_loan_token_change(
        tx_builder,
        policy_ref=_required(refs.loan_policy, "the loan-token policy script"),
        loan_policy=loan_policy,
        current_name=loan.token_name,
        new_name=new_name,
    )
    if action is not None and plan.deltas is not None:
        add_action_leg(
            tx_builder,
            action=action,
            deltas=plan.deltas,
            script_ref=_required(refs.action, "the action validator script"),
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
            loan_output(
                address=script_payment_address(
                    _required(params.loan_validator, "a loan validator"),
                ),
                datum=encode_loan_datum(
                    plan.loan,
                    indefinite=bool(snapshot.indefinite),
                ),
                loan_policy=loan_policy,
                loan_token=new_name,
                collateral=plan.collateral or {},
                previous_lovelace=(
                    loan.utxo.lovelace
                    if snapshot.loan_lovelace is None
                    else snapshot.loan_lovelace
                ),
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
    if upper is not None:
        merge_validity(
            tx_builder,
            upper_slot=posix_ms_to_slot(upper),
            lower_slot=(
                None
                if snapshot.valid_from_ms is None
                else posix_ms_to_slot(snapshot.valid_from_ms)
            ),
        )
