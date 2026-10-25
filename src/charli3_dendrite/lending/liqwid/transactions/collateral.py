"""Liqwid V2 collateral change: add, remove or swap a loan's collateral.

The transaction spends the loan by the loan validator and the owner's funding,
burns the loan token's current name and mints the flipped one, and re-creates the
loan at the ``p[35]`` enterprise address with the target collateral. The record
written back is the input record when the loan index is at or above the market's
``s5``, and the settled record otherwise (`rebase_for_collateral`). It spends no
action UTxO, so it runs no action script and writes no validity window.

References: the loan validator, loan-token policy and borrow staking scripts, the
market's MarketParams and MarketState, and the health set of the output collateral.
The loan validator checks health whenever a quantity decreases and on any change
without a repay or a borrow, against the debt ``p0 + max(iS, m0)``.

Collateral the target adds comes from the funding, and collateral it removes goes
back through the caller's change output: the builder adds no wallet output.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.transactions._flow import kept_loan_lovelace
from charli3_dendrite.lending.liqwid.transactions._flow import loan_target
from charli3_dendrite.lending.liqwid.transactions._flow import market_references
from charli3_dendrite.lending.liqwid.transactions._flow import written_loan
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.capture import sole
from charli3_dendrite.lending.liqwid.transactions.legs import add_funding
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_input
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_token_change
from charli3_dendrite.lending.liqwid.transactions.legs import finish_loan_flow
from charli3_dendrite.lending.liqwid.transactions.legs import flip_name
from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.liqwid.transactions.terms import check_market_limits
from charli3_dendrite.lending.liqwid.transactions.terms import health_debt
from charli3_dendrite.lending.liqwid.transactions.terms import held_collateral
from charli3_dendrite.lending.liqwid.transactions.terms import rebase_for_collateral
from charli3_dendrite.lending.liqwid.transactions.terms import settle
from charli3_dendrite.lending.transactions.common import merge_reference_inputs
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.transactions.utxos import Utxo

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum

__all__ = ["CollateralSnapshot", "build_collateral"]


def _check_collateral_count(
    params: MarketParamsDatum,
    collateral: Mapping[str, int],
) -> None:
    """``p[14]``: the output loan holds at most that many collateral units."""
    check_market_limits(
        Flow.COLLATERAL,
        params=params,
        amount=0,
        output_collateral_count=len(held_collateral(collateral)),
    )


def _check_change(loan: LoanRecord, collateral: Mapping[str, int]) -> None:
    """Refuse a target that holds exactly the loan's collateral.

    Raises:
        ValueError: A target quantity is negative, or the target is the loan's
            collateral (the change would only flip the loan-token name).
    """
    if held_collateral(collateral) == loan.collateral:
        msg = f"the target collateral {dict(collateral)} is the loan's current one"
        raise ValueError(msg)


@dataclass
class CollateralSnapshot(PoolActionSnapshot):
    """Everything a collateral change of one loan needs.

    ``collateral`` is the target: collateral unit -> quantity of the output loan
    (a unit absent or at zero is removed). ``loan_lovelace`` replaces the input
    loan's lovelace as the least the output keeps (a replay pins the captured
    amount); the output never holds less than the ledger minimum. ``indefinite``
    writes the loan record in the indefinite style (a replay of a captured style;
    None and False write it definite). ``deployment`` is the Liqwid deployment the
    health set and the reference hygiene check read (the bundled one when None).
    """

    loan: LoanRecord
    market: MarketComponents
    records: MarketRecords
    script_refs: LoanScriptRefs
    health: HealthInputs
    owner_address: str
    collateral: dict[str, int]
    funding: list[Utxo]
    loan_lovelace: int | None = None
    deployment: LiqwidDeployment | None = None
    indefinite: bool | None = None

    @classmethod
    def from_capture(cls, fix: dict[str, Any]) -> CollateralSnapshot:
        """Rebuild the snapshot of a captured mainnet collateral change.

        The loan is the input at a loan validator; every other input is funding,
        and the owner address is the first funding input's (the first other output's
        when there is none). Records, feeds and scripts come from the captured
        reference inputs. The target collateral, the loan lovelace and the record
        style are the captured output loan's.

        Raises:
            ValueError: The capture spends or creates other than one loan.
        """
        capture = CapturedTx.of(fix)
        loan: LoanRecord = sole(capture.loan_inputs(), "loan input")
        output: LoanRecord = sole(capture.loan_outputs(), "loan output")
        borrow = capture.market_records(loan.market)
        target = output.collateral
        return cls(
            loan=loan,
            market=loan.market,
            records=borrow,
            script_refs=capture.loan_script_refs(loan.market, borrow.params_datum),
            health=capture.health(held_collateral(target), borrow=borrow),
            owner_address=capture.wallet_address(loan.utxo, output.utxo),
            collateral=target,
            funding=capture.funding(loan.utxo),
            loan_lovelace=output.utxo.lovelace,
            indefinite=capture.indefinite(output.utxo),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: str,
        owner_address: str,
        collateral: Mapping[str, int],
        funding: Sequence[Utxo] | None = None,
        deployment: LiqwidDeployment | None = None,
        allow_spent: bool = False,
    ) -> CollateralSnapshot:
        """Resolve a change of the loan at ``loan_utxo`` to ``collateral``.

        ``collateral`` is the target (unit -> quantity of the output loan).
        ``funding`` defaults to the unspent UTxOs at ``owner_address``.
        ``allow_spent`` resolves an already consumed loan (a captured replay). A
        negative target quantity is refused before anything is read, a target equal
        to the loan's collateral once the loan is read, and the output collateral
        count (``p[14]``) before the scripts and the health inputs are read.

        Raises:
            ValueError: A target quantity is negative, or the target is the loan's
                collateral.
            UnsupportedLoanError: The loan is legacy-generation or no loan.
            UnsupportedMarketError: The market's MarketParams are shorter than 36
                fields, or a script or record cannot be resolved.
            CollateralCountError: The target holds more than ``p[14]`` units.
            UnsupportedCollateralError: A target unit has no exact
                CollateralParams record of the loan's market.
            MissingFeedError: No feed prices an underlying.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_health_inputs,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_loan_script_refs,
        )
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_owner_funding,
        )

        target = dict(collateral)
        held_collateral(target)
        loan = resolve_loan(
            backend,
            loan_utxo,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        _check_change(loan, target)
        market = loan.market
        borrow = resolve_market(backend, market, deployment=deployment)
        _check_collateral_count(borrow.params_datum, target)
        script_refs = resolve_loan_script_refs(
            backend,
            market,
            borrow.params_datum,
            deployment=deployment,
        )
        health = resolve_health_inputs(
            backend,
            held_collateral(target),
            borrow=borrow,
            deployment=deployment,
        )
        return cls(
            loan=loan,
            market=market,
            records=borrow,
            script_refs=script_refs,
            health=health,
            owner_address=owner_address,
            collateral=target,
            funding=resolve_owner_funding(backend, owner_address, funding),
            deployment=deployment,
        )


def build_collateral(
    tx_builder: TransactionBuilder,
    *,
    snapshot: CollateralSnapshot,
) -> None:
    """Add the collateral change ``snapshot`` describes to ``tx_builder``.

    The target, the collateral count and the market's loan scripts are checked
    before anything is added; the health check and the reference hygiene run after
    the legs are added, so discard the builder when they raise.

    Raises:
        ValueError: A target quantity is negative, or the target is the loan's
            collateral.
        CollateralCountError: The target holds more than ``p[14]`` units.
        UnsupportedMarketError: The market's MarketParams name no loan validator,
            or it has no current loan-token policy.
        UnsupportedCollateralError: A target unit has no exact CollateralParams
            record of the loan's market.
        UnhealthyLoanError: The output loan fails the health rule.
        ForeignReferenceInputError: The builder references an input a loan
            validator would reject.
    """
    _build_collateral(tx_builder, snapshot, stash=snapshot)


def _build_collateral(
    tx_builder: TransactionBuilder,
    snapshot: CollateralSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """:func:`build_collateral`, stashing the funding on ``stash``."""
    records, loan, refs = snapshot.records, snapshot.loan, snapshot.script_refs
    _check_collateral_count(records.params_datum, snapshot.collateral)
    _check_change(loan, snapshot.collateral)
    target = loan_target(records)
    s5 = records.state_datum.index
    settled = settle(loan.datum, s5)
    new_name = flip_name(loan.token_name)

    add_funding(tx_builder, stash, snapshot.funding)
    add_loan_input(tx_builder, loan=loan.utxo, script_ref=refs.loan_validator)
    add_loan_token_change(
        tx_builder,
        policy_ref=refs.loan_policy,
        loan_policy=target.policy,
        current_name=loan.token_name,
        new_name=new_name,
    )
    tx_builder.add_output(
        written_loan(
            target,
            datum=rebase_for_collateral(loan.datum, s5),
            indefinite=snapshot.indefinite,
            loan_token=new_name,
            collateral=snapshot.collateral,
            previous_lovelace=kept_loan_lovelace(loan, snapshot.loan_lovelace),
        ),
    )
    merge_reference_inputs(tx_builder, market_references(records))
    finish_loan_flow(
        tx_builder,
        records=records,
        script_refs=refs,
        health=snapshot.health,
        owner=loan.datum.owner,
        collateral_in=loan.collateral,
        collateral_out=snapshot.collateral,
        debt=health_debt(
            loan.datum.principal,
            settled.interest,
            loan.datum.min_interest,
            0,
            0,
        ),
        borrow=0,
        repay=0,
        deployment=resolve_deployment(snapshot.deployment),
    )
