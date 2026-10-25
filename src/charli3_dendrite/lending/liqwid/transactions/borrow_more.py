"""Liqwid V2 borrow more: add principal to an open loan.

The loan is settled to the market index (``p1 = p0 + A``, ``i1 = iS``,
``m1 = m0 + ceil(A * p15)``, ``x1``), the action UTxO records ``+A`` of principal
and pays it out, the loan-token name flips (``00``/``01`` swap, ``30``/``31`` become
``01``) and the loan output keeps the owner and at least its lovelace. It holds the
loan's collateral, or a caller's target. Besides an open's references it runs the
loan validator from its reference script; the health check measures
``p0 + max(iS, m0) + A`` against the output collateral.
"""

from __future__ import annotations

from collections.abc import Collection
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
from charli3_dendrite.lending.liqwid.transactions.borrow import LoanChange
from charli3_dendrite.lending.liqwid.transactions.borrow import add_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow import resolve_borrow
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.capture import sole
from charli3_dendrite.lending.liqwid.transactions.legs import flip_name
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.liqwid.transactions.terms import health_debt
from charli3_dendrite.lending.liqwid.transactions.terms import held_collateral
from charli3_dendrite.lending.liqwid.transactions.terms import increase
from charli3_dendrite.lending.liqwid.transactions.terms import settle
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot

if TYPE_CHECKING:
    import random

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = ["IncreaseSnapshot", "build_increase"]


@dataclass
class IncreaseSnapshot(PoolActionSnapshot):
    """Everything a borrow more of ``amount`` on ``loan`` needs, resolved.

    ``owner`` is the loan's owner (it signs and stays the owner) and
    ``owner_address`` the wallet ``funding`` comes from. ``collateral`` is the
    output loan's collateral target, None to keep the loan's. ``loan_lovelace``
    pins the loan output's lovelace (a replay; None keeps at least the loan's) and
    ``indefinite`` the datums' array style (a replay; None writes definite arrays).
    """

    loan: LoanRecord
    market: MarketComponents
    records: MarketRecords
    action: ActionUtxo
    script_refs: LiqwidScriptRefs
    health: HealthInputs
    owner: str
    owner_address: str
    amount: int
    collateral: dict[str, int] | None
    funding: list[Utxo]
    valid_to_ms: int
    valid_from_ms: int | None = None
    loan_lovelace: int | None = None
    deployment: LiqwidDeployment | None = None
    indefinite: bool | None = None

    @property
    def collateral_out(self) -> dict[str, int]:
        """The output loan's collateral: the target, else the loan's."""
        target = self.loan.collateral if self.collateral is None else self.collateral
        return held_collateral(target)

    @classmethod
    def from_capture(
        cls,
        fix: Mapping[str, Any],
        *,
        deployment: LiqwidDeployment | None = None,
    ) -> IncreaseSnapshot:
        """Rebuild the snapshot of a captured borrow more, for a byte-exact replay.

        The captured action UTxO, funding, validity window, output collateral,
        loan lovelace and datum style are pinned.
        """
        capture = CapturedTx.of(fix, deployment=deployment)
        loan: LoanRecord = sole(capture.loan_inputs(), "loan input")
        written: LoanRecord = sole(capture.loan_outputs(), "loan output")
        market = loan.market
        records = capture.market_records(market)
        action: ActionUtxo = sole(capture.action_inputs(market), "action input")
        recreated: ActionUtxo = sole(capture.action_outputs(market), "action output")
        collateral = written.collateral
        funding = capture.funding(action.utxo, loan.utxo)
        return cls(
            loan=loan,
            market=market,
            records=records,
            action=action,
            script_refs=capture.script_refs(market, records.params_datum),
            health=capture.health(collateral, borrow=records),
            owner=loan.datum.owner,
            owner_address=capture.wallet_address(
                action.utxo,
                loan.utxo,
                recreated.utxo,
                written.utxo,
            ),
            amount=ActionDeltas.between(action.datum, recreated.datum).principal,
            collateral=collateral,
            funding=funding,
            valid_to_ms=capture.upper_bound_ms(),
            valid_from_ms=capture.valid_from_ms,
            loan_lovelace=written.utxo.lovelace,
            deployment=deployment,
            indefinite=capture.indefinite(written.utxo),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: str,
        owner_address: str,
        amount: int,
        collateral: Mapping[str, int] | None = None,
        action_utxo: str | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
        allow_spent: bool = False,
    ) -> IncreaseSnapshot:
        """Resolve a borrow more of ``amount`` on the loan at ``loan_utxo``.

        The loan's market is the borrow market; ``collateral`` is the output loan's
        collateral target (None keeps the loan's). ``owner_address``'s UTxOs fund
        the transaction unless ``funding`` is given. The checks and their order are
        :func:`~charli3_dendrite.lending.liqwid.transactions.borrow.resolve_borrow`'s,
        with the health debt ``p0 + max(iS, m0) + A`` at the current index.

        Raises:
            UnsupportedLoanError: The out-ref is no current-generation loan.
            LiqwidBuildError: As ``resolve_borrow``.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market

        active = resolve_deployment(deployment)
        loan = resolve_loan(
            backend,
            loan_utxo,
            deployment=active,
            allow_spent=allow_spent,
        )
        market = loan.market
        borrow = resolve_market(backend, market, deployment=active)
        target = held_collateral(loan.collateral if collateral is None else collateral)
        resolved = resolve_borrow(
            backend,
            flow=Flow.INCREASE,
            records=borrow,
            owner_address=owner_address,
            amount=amount,
            collateral_out=target,
            debt=_debt(loan, borrow, amount),
            action_utxo=action_utxo,
            exclude=exclude,
            valid_to_ms=valid_to_ms,
            funding=funding,
            rng=rng,
            deployment=active,
        )
        return cls(
            loan=loan,
            market=market,
            records=borrow,
            action=resolved.action,
            script_refs=resolved.script_refs,
            health=resolved.health,
            owner=loan.datum.owner,
            owner_address=owner_address,
            amount=amount,
            collateral=None if collateral is None else target,
            funding=resolved.funding,
            valid_to_ms=resolved.valid_to_ms,
            deployment=deployment,
        )


def _debt(loan: LoanRecord, records: MarketRecords, amount: int) -> int:
    """The health debt of borrowing ``amount`` more, at the market's index."""
    datum = loan.datum
    settled = settle(datum, records.state_datum.index)
    return health_debt(
        datum.principal,
        settled.interest,
        datum.min_interest,
        amount,
        0,
    )


def build_increase(
    tx_builder: TransactionBuilder,
    *,
    snapshot: IncreaseSnapshot,
) -> None:
    """Add the borrow more of ``snapshot`` to ``tx_builder`` (funding stashed on it).

    The caller balances, signs and submits; the borrowed amount, any collateral
    removed and the change reach the caller through balancing.
    """
    _build_increase(tx_builder, snapshot, stash=snapshot)


def _build_increase(
    tx_builder: TransactionBuilder,
    snapshot: IncreaseSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """Add the borrow more of ``snapshot``, stashing its funding on ``stash``."""
    loan = snapshot.loan
    datum, deltas = increase(
        loan.datum,
        snapshot.records.state_datum.index,
        snapshot.amount,
        snapshot.records.params_datum,
    )
    add_borrow(
        tx_builder,
        snapshot,
        stash=stash,
        change=LoanChange(
            flow=Flow.INCREASE,
            loan=loan,
            datum=datum,
            deltas=deltas,
            collateral_in=loan.collateral,
            collateral_out=snapshot.collateral_out,
            debt=_debt(loan, snapshot.records, snapshot.amount),
            token_name=flip_name(loan.token_name),
            previous_lovelace=kept_loan_lovelace(loan, snapshot.loan_lovelace),
        ),
    )
