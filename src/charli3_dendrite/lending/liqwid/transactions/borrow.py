"""Liqwid V2 borrow: open a loan against collateral qTokens.

An open spends one of the borrow market's action UTxOs, which records the new
principal ``A`` (deltas ``[0, 0, A, 0, 0]``) and pays it out; mints the loan token
``01``; and writes the loan ``[owner, A, 0, ceil(A * p15), s5]`` holding the
collateral at the market's loan validator (``p[35]``, enterprise address, the least
lovelace). It references the action, loan-token policy and borrow staking scripts,
the market's MarketParams and MarketState and the health set of the collateral,
requires the owner's signature, withdraws zero from the borrow staking script and
ends by the market's batch deadline. The legs add no payout or change output: the
borrowed amount and the wallet's change reach the caller through balancing.

:func:`add_borrow` is the body an open shares with a borrow more
(:mod:`~charli3_dendrite.lending.liqwid.transactions.borrow_more`), and
:func:`resolve_borrow` the live resolution they share. Limits are checked in a fixed
order: the market-level limits before an action UTxO is chosen, then the chosen
UTxO's caps and liquidity, then health.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from pycardano import Address
from pycardano import TransactionBuilder
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.transactions._flow import check_flow_limits
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
from charli3_dendrite.lending.liqwid.transactions.legs import check_loan_health
from charli3_dendrite.lending.liqwid.transactions.legs import finish_loan_flow
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.liqwid.transactions.terms import check_market_limits
from charli3_dendrite.lending.liqwid.transactions.terms import held_collateral
from charli3_dendrite.lending.liqwid.transactions.terms import open_loan
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot

if TYPE_CHECKING:
    import random

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.datums import LoanDatum
    from charli3_dendrite.lending.liqwid.transactions.borrow_more import (
        IncreaseSnapshot,
    )
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "OPEN_TOKEN_NAME",
    "BorrowSnapshot",
    "LoanChange",
    "ResolvedBorrow",
    "add_borrow",
    "build_borrow",
    "owner_key_hash",
    "resolve_borrow",
]

# The loan-token name an open mints.
OPEN_TOKEN_NAME = "01"


def owner_key_hash(address: str) -> str:
    """The payment key hash (hex) of ``address``, the owner a loan records.

    Raises:
        ValueError: The address's payment part is not a key hash.
    """
    payment = Address.decode(address).payment_part
    if not isinstance(payment, VerificationKeyHash):
        msg = f"loan owner address {address} has no payment key hash"
        raise ValueError(msg)
    return payment.payload.hex()


@dataclass
class BorrowSnapshot(PoolActionSnapshot):
    """Everything an open of a loan on ``market`` needs, resolved.

    ``owner`` is the borrower's payment key hash and ``owner_address`` the wallet
    the ``funding`` (collateral qTokens and fees) comes from. ``collateral`` maps
    each collateral unit to the quantity the loan holds. ``loan_lovelace`` pins the
    loan's lovelace (a replay; None writes the least) and ``indefinite`` the
    datums' array style (a replay; None writes definite arrays). ``deployment``
    is the Liqwid deployment (the bundled one when None).
    """

    market: MarketComponents
    records: MarketRecords
    action: ActionUtxo
    script_refs: LiqwidScriptRefs
    health: HealthInputs
    owner: str
    owner_address: str
    amount: int
    collateral: dict[str, int]
    funding: list[Utxo]
    valid_to_ms: int
    valid_from_ms: int | None = None
    loan_lovelace: int | None = None
    loan_token: str = OPEN_TOKEN_NAME
    deployment: LiqwidDeployment | None = None
    indefinite: bool | None = None

    @classmethod
    def from_capture(
        cls,
        fix: Mapping[str, Any],
        *,
        deployment: LiqwidDeployment | None = None,
    ) -> BorrowSnapshot:
        """Rebuild the snapshot of a captured open, for a byte-exact replay.

        The captured action UTxO, funding, validity window, loan lovelace, loan
        token name and datum style are pinned.

        Raises:
            ValueError: The capture does not write one loan and spend one action
                UTxO of its market, references no action validator of it, or has
                no upper bound.
        """
        capture = CapturedTx.of(fix, deployment=deployment)
        loan: LoanRecord = sole(capture.loan_outputs(), "loan output")
        market = loan.market
        records = capture.market_records(market)
        action: ActionUtxo = sole(capture.action_inputs(market), "action input")
        outputs = [a.utxo for a in capture.action_outputs(market)]
        collateral = loan.collateral
        funding = capture.funding(action.utxo)
        return cls(
            market=market,
            records=records,
            action=action,
            script_refs=capture.script_refs(market, records.params_datum),
            health=capture.health(collateral, borrow=records),
            owner=loan.datum.owner,
            owner_address=capture.wallet_address(action.utxo, loan.utxo, *outputs),
            amount=loan.datum.principal,
            collateral=collateral,
            funding=funding,
            valid_to_ms=capture.upper_bound_ms(),
            valid_from_ms=capture.valid_from_ms,
            loan_lovelace=loan.utxo.lovelace,
            loan_token=loan.token_name,
            deployment=deployment,
            indefinite=capture.indefinite(loan.utxo),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market: str,
        owner_address: str,
        amount: int,
        collateral: Mapping[str, int],
        action_utxo: str | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
    ) -> BorrowSnapshot:
        """Resolve an open of ``amount`` on ``market`` against ``collateral``.

        The owner is the payment key of ``owner_address``, whose UTxOs fund the
        transaction unless ``funding`` is given. The action UTxO is ``action_utxo``
        when pinned, else a random eligible one not in ``exclude``
        (:func:`resolve_borrow` lists the checks and their order).

        Raises:
            ValueError: ``owner_address`` has no payment key hash.
            LiqwidBuildError: As :func:`resolve_borrow`.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market

        active = resolve_deployment(deployment)
        borrow_market = active.market_by_name(market)
        owner = owner_key_hash(owner_address)
        records = resolve_market(backend, borrow_market, deployment=active)
        target = held_collateral(collateral)
        resolved = resolve_borrow(
            backend,
            flow=Flow.OPEN,
            records=records,
            owner_address=owner_address,
            amount=amount,
            collateral_out=target,
            debt=amount,  # a new loan owes p0 + max(iS, m0) = 0 before the borrow
            action_utxo=action_utxo,
            exclude=exclude,
            valid_to_ms=valid_to_ms,
            funding=funding,
            rng=rng,
            deployment=active,
        )
        return cls(
            market=borrow_market,
            records=records,
            action=resolved.action,
            script_refs=resolved.script_refs,
            health=resolved.health,
            owner=owner,
            owner_address=owner_address,
            amount=amount,
            collateral=target,
            funding=resolved.funding,
            valid_to_ms=resolved.valid_to_ms,
            deployment=deployment,
        )


@dataclass(frozen=True)
class ResolvedBorrow:
    """What :func:`resolve_borrow` resolves for an open or a borrow more."""

    script_refs: LiqwidScriptRefs
    action: ActionUtxo
    health: HealthInputs
    funding: list[Utxo]
    valid_to_ms: int


def resolve_borrow(
    backend: AbstractBackend,
    *,
    flow: Flow,
    records: MarketRecords,
    owner_address: str,
    amount: int,
    collateral_out: Mapping[str, int],
    debt: int,
    action_utxo: str | None,
    exclude: Collection[str],
    valid_to_ms: int | None,
    funding: Sequence[Utxo] | None,
    rng: random.Random | None,
    deployment: LiqwidDeployment,
) -> ResolvedBorrow:
    """Resolve and pre-check a borrow of ``amount`` on the market of ``records``.

    In order: the market's loan scripts (a market unfit for loans is refused
    before anything else); the market-level limits (``p[1]`` on ``amount``,
    ``p[14]`` on ``collateral_out``); the validity bound (``valid_to_ms`` or the
    default, capped at the batch deadline); the action UTxO (``action_utxo`` when
    pinned, else a random one not in ``exclude`` whose caps and liquidity admit
    the borrow); the health inputs of ``collateral_out`` and the health of a loan
    owing ``debt``; then the funding (``owner_address``'s UTxOs unless
    ``funding`` is given).

    Raises:
        UnsupportedMarketError: The market has no current loan scripts.
        MinValueError, CollateralCountError: A market-level limit fails.
        MarketAwaitingBatchError: No upper bound is left before the deadline.
        ActionUtxoSpentError, CapExceededError, InsufficientLiquidityError: No
            action UTxO can serve the borrow.
        UnsupportedCollateralError, MissingFeedError, UnhealthyLoanError: The
            collateral cannot be valued or does not cover ``debt``.
    """
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_flow_action
    from charli3_dendrite.lending.liqwid.transactions.resolve import (
        resolve_health_inputs,
    )
    from charli3_dendrite.lending.liqwid.transactions.resolve import (
        resolve_owner_funding,
    )
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_script_refs
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_valid_to_ms

    params = records.params_datum
    script_refs = resolve_script_refs(
        backend,
        records.market,
        params,
        deployment=deployment,
    )
    held = held_collateral(collateral_out)
    check_market_limits(
        flow,
        params=params,
        amount=amount,
        output_collateral_count=len(held),
    )
    upper = resolve_valid_to_ms(backend, records, valid_to_ms)
    action = resolve_flow_action(
        backend,
        records,
        flow=flow,
        deltas=ActionDeltas(principal=amount),
        pin=action_utxo,
        exclude=exclude,
        rng=rng,
    )
    health = resolve_health_inputs(backend, held, borrow=records, deployment=deployment)
    check_loan_health(records, health, held, debt, deployment)
    return ResolvedBorrow(
        script_refs=script_refs,
        action=action,
        health=health,
        funding=resolve_owner_funding(backend, owner_address, funding),
        valid_to_ms=upper,
    )


@dataclass(frozen=True)
class LoanChange:
    """The loan a borrow writes, and what its health check measures.

    ``loan`` is the input loan (None on an open) and ``datum`` the loan record
    written; ``deltas`` are the action UTxO's; ``debt`` is the health debt
    (`~charli3_dendrite.lending.liqwid.transactions.terms.health_debt`);
    ``token_name`` the loan-token name written and ``previous_lovelace`` the least
    lovelace the loan output keeps (None on an open).
    """

    flow: Flow
    loan: LoanRecord | None
    datum: LoanDatum
    deltas: ActionDeltas
    collateral_in: Mapping[str, int]
    collateral_out: Mapping[str, int]
    debt: int
    token_name: str
    previous_lovelace: int | None


def add_borrow(
    tx_builder: TransactionBuilder,
    snapshot: BorrowSnapshot | IncreaseSnapshot,
    *,
    stash: PoolActionSnapshot,
    change: LoanChange,
) -> None:
    """Add a borrow of ``snapshot.amount`` writing ``change`` to ``tx_builder``.

    Before anything is added: the market's loan validator address and loan-token
    policy (`loan_target`), the reference scripts the borrow runs (the action
    validator, the loan-token policy, the borrow staking script and, when
    ``change`` spends a loan, the loan validator), the flow's limits on the
    snapshot's action UTxO and the loan output. Then the funding is spent (and
    stashed on ``stash``), the input loan (if any) spent by the loan validator,
    the action UTxO re-created with ``change.deltas``, the loan token minted (and
    the input's burnt), the loan output written, the flow finished
    (`finish_loan_flow`: health set and check, owner signer, borrow staking
    withdrawal, reference hygiene) and the validity window merged, its upper
    bound capped at the batch deadline. The health check and the reference
    hygiene run after the legs are added: discard the builder when they raise.

    Raises:
        UnsupportedMarketError: The market's MarketParams name no loan validator,
            the market has no current loan-token policy, or a script the borrow
            runs is missing from ``snapshot.script_refs``.
        LiqwidBuildError: A limit or the health rule fails.
    """
    market, records = snapshot.market, snapshot.records
    params, state = records.params_datum, records.state_datum
    target = loan_target(records)
    refs = snapshot.script_refs
    action_script = required_script(refs.action, market, "action validator")
    loan_policy = required_script(refs.loan_policy, market, "loan-token policy")
    required_script(refs.borrow_staking, market, "borrow staking")
    loan_validator = (
        None
        if change.loan is None
        else required_script(refs.loan_validator, market, "loan validator")
    )
    held = held_collateral(change.collateral_out)
    check_flow_limits(
        change.flow,
        records=records,
        action=snapshot.action,
        deltas=change.deltas,
        amount=snapshot.amount,
        output_collateral_count=len(held),
    )
    output = written_loan(
        target,
        datum=change.datum,
        indefinite=snapshot.indefinite,
        loan_token=change.token_name,
        collateral=held,
        previous_lovelace=change.previous_lovelace,
    )
    add_funding(tx_builder, stash, snapshot.funding)
    if change.loan is not None and loan_validator is not None:
        add_loan_input(tx_builder, loan=change.loan.utxo, script_ref=loan_validator)
    add_action_leg(
        tx_builder,
        action=snapshot.action,
        deltas=change.deltas,
        script_ref=action_script,
        references=market_references(records),
        underlying=market.underlying,
        indefinite=snapshot.indefinite,
    )
    add_loan_token_change(
        tx_builder,
        policy_ref=loan_policy,
        loan_policy=target.policy,
        current_name=None if change.loan is None else change.loan.token_name,
        new_name=change.token_name,
    )
    tx_builder.add_output(output)
    finish_loan_flow(
        tx_builder,
        records=records,
        script_refs=refs,
        health=snapshot.health,
        owner=snapshot.owner,
        collateral_in=change.collateral_in,
        collateral_out=held,
        debt=change.debt,
        borrow=snapshot.amount,
        repay=0,
        deployment=resolve_deployment(snapshot.deployment),
    )
    merge_validity_ms(
        tx_builder,
        upper_ms=min(snapshot.valid_to_ms, ttl_cap_ms(state, params)),
        lower_ms=snapshot.valid_from_ms,
    )


def build_borrow(tx_builder: TransactionBuilder, *, snapshot: BorrowSnapshot) -> None:
    """Add the open of ``snapshot`` to ``tx_builder`` (the funding stashed on it).

    The caller balances, signs and submits; the borrowed amount and any change
    reach the caller through balancing.

    Raises:
        UnsupportedMarketError: The market has no current loan scripts, or a
            script the open runs is missing from ``snapshot.script_refs``.
        LiqwidBuildError: A limit or the health rule fails (as `add_borrow`).
        ValidityWindowError: The merged validity window is empty.
    """
    _build_borrow(tx_builder, snapshot, stash=snapshot)


def _build_borrow(
    tx_builder: TransactionBuilder,
    snapshot: BorrowSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """Add the open of ``snapshot``, stashing its funding on ``stash``."""
    state = snapshot.records.state_datum
    datum = open_loan(
        snapshot.owner,
        snapshot.amount,
        state.index,
        snapshot.records.params_datum,
    )
    add_borrow(
        tx_builder,
        snapshot,
        stash=stash,
        change=LoanChange(
            flow=Flow.OPEN,
            loan=None,
            datum=datum,
            deltas=ActionDeltas(principal=snapshot.amount),
            collateral_in={},
            collateral_out=snapshot.collateral,
            debt=snapshot.amount,  # nothing owed before the borrow
            token_name=snapshot.loan_token,
            previous_lovelace=snapshot.loan_lovelace,
        ),
    )
