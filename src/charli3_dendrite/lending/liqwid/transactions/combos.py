"""Liqwid V2 combinations: supply-and-borrow and remove-and-redeem.

Each combination is the exact union of two legs built by their own flows:

- **Supply-and-borrow** supplies ``A`` to market X (spending one of X's action
  UTxOs and minting ``Q = floor(A / s9)`` qX) and opens a loan on market Y (spending
  one of Y's action UTxOs) whose collateral is exactly those ``Q`` qX.
- **Remove-and-redeem** takes ``Q`` qX out of a loan on market Y (a collateral
  change) and redeems them on market X (spending one of X's action UTxOs), burning
  them straight out of the loan: no wallet input or output holds them.

The action-only leg is added first and the loan leg last, so the loan flow's end
(health set and check, owner signer, borrow staking withdrawal, reference hygiene)
covers the whole transaction. The funding is spent and stashed once, on the
combination's snapshot; the references merge by out-ref (the first kept), so a
market both legs read is referenced once. Both legs must read the same MarketState
of such a market (and, within one market, the same MarketParams).

The validity window ends by both markets' batch deadlines ``s8 + p[3]``: a supply
or redeem leg refuses a bound past its market's, the open caps its bound at the
borrow market's, and a remove-and-redeem caps the window at the loan market's.
When X and Y are the same market, the two legs spend two different action UTxOs
of it, each checked against its own caps.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.transactions._flow import merge_validity_ms
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import _build_borrow
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import _build_collateral
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import _build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import _build_supply
from charli3_dendrite.lending.liqwid.transactions.terms import held_collateral
from charli3_dendrite.lending.liqwid.transactions.terms import ttl_cap_ms
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.units import POLICY_HEX_LEN

if TYPE_CHECKING:
    import random

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
    from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "RemoveAndRedeemSnapshot",
    "SupplyAndBorrowSnapshot",
    "build_remove_and_redeem",
    "build_supply_and_borrow",
]


# --- snapshots ---------------------------------------------------------------


@dataclass
class SupplyAndBorrowSnapshot(PoolActionSnapshot):
    """A supply to one market and an open on another (or the same) market.

    Attributes:
        supply: The supply leg; its qTokens are the loan's collateral.
        borrow: The open leg; its collateral is exactly ``{qX: supply.qtokens}``.
    """

    supply: SupplySnapshot
    borrow: BorrowSnapshot

    @classmethod
    def from_capture(cls, fix: dict[str, Any]) -> SupplyAndBorrowSnapshot:
        """Rebuild the snapshot of a captured supply-and-borrow (byte-exact replay).

        Each leg pins what its own ``from_capture`` pins (the action UTxO, amounts,
        loan lovelace, signers, validity bounds and datum style); both legs share
        the wallet inputs (every input but the two action UTxOs) and the wallet
        address.

        Raises:
            ValueError: The capture is not a supply to one market and an open on
                another.
        """
        capture = CapturedTx.of(fix)
        supply = SupplySnapshot.from_capture(fix)
        borrow = BorrowSnapshot.from_capture(fix)
        spent = (supply.action.utxo, borrow.action.utxo)
        written = [
            *(a.utxo for a in capture.action_outputs(supply.market)),
            *(a.utxo for a in capture.action_outputs(borrow.market)),
            *(loan.utxo for loan in capture.loan_outputs()),
        ]
        owner_address = capture.wallet_address(*spent, *written)
        funding = capture.funding(*spent)
        return cls(
            supply=dataclasses.replace(
                supply,
                owner_address=owner_address,
                funding=list(funding),
            ),
            borrow=dataclasses.replace(
                borrow,
                owner_address=owner_address,
                funding=list(funding),
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        supply_market: str,
        borrow_market: str,
        owner_address: str,
        supply_amount: int,
        borrow_amount: int,
        action_utxos: tuple[str | None, str | None] = (None, None),
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
    ) -> SupplyAndBorrowSnapshot:
        """Resolve a supply of ``supply_amount`` and an open of ``borrow_amount``.

        The supply leg is resolved as `SupplySnapshot.from_backend` on
        ``supply_market``; the open as `BorrowSnapshot.from_backend` on
        ``borrow_market`` against the qTokens the supply mints, funded by the same
        wallet UTxOs. ``action_utxos`` pins the supply's and the open's action
        UTxOs. When the two markets are the same, the open's choice also excludes
        the supply's action UTxO, and the supply's choice excludes the open's
        pinned one. Both legs end at the earlier of their bounds.

        Raises:
            ValueError: The same market's action UTxO is pinned for both legs.
            KeyError: No market has one of the names.
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        active = resolve_deployment(deployment)
        same = (
            active.market_by_name(supply_market).market_id
            == active.market_by_name(borrow_market).market_id
        )
        supply_pin, borrow_pin = action_utxos
        if same and supply_pin is not None and supply_pin == borrow_pin:
            msg = (
                f"a supply-and-borrow on one market spends two different action "
                f"UTxOs, not {supply_pin} twice"
            )
            raise ValueError(msg)
        supply_exclude = (
            {*exclude, borrow_pin} if same and borrow_pin is not None else exclude
        )
        supply = SupplySnapshot.from_backend(
            backend,
            market=supply_market,
            owner_address=owner_address,
            amount=supply_amount,
            action_utxo=supply_pin,
            exclude=supply_exclude,
            valid_to_ms=valid_to_ms,
            funding=funding,
            rng=rng,
            deployment=deployment,
        )
        borrow = BorrowSnapshot.from_backend(
            backend,
            market=borrow_market,
            owner_address=owner_address,
            amount=borrow_amount,
            collateral={supply.market.qtoken_policy: supply.qtokens},
            action_utxo=borrow_pin,
            exclude={*exclude, supply.action.out_ref} if same else exclude,
            valid_to_ms=valid_to_ms,
            funding=supply.funding,
            rng=rng,
            deployment=deployment,
        )
        upper = min(supply.valid_to_ms, borrow.valid_to_ms)
        supply.valid_to_ms = borrow.valid_to_ms = upper
        return cls(supply=supply, borrow=borrow)


@dataclass
class RemoveAndRedeemSnapshot(PoolActionSnapshot):
    """qTokens taken out of a loan and redeemed in the same transaction.

    Attributes:
        collateral: The collateral change of the loan; its target holds ``Q`` fewer
            of the redeemed market's qTokens than the loan.
        redeem: The redeem of those ``Q`` qTokens; the wallet holds none of them.
    """

    collateral: CollateralSnapshot
    redeem: RedeemSnapshot

    @classmethod
    def from_capture(cls, fix: dict[str, Any]) -> RemoveAndRedeemSnapshot:
        """Rebuild the snapshot of a captured remove-and-redeem (byte-exact replay).

        Each leg pins what its own ``from_capture`` pins (the loan, the target
        collateral, the action UTxO, the burn and payout, signers, the validity
        bound and datum styles); both legs share the wallet inputs (every input but
        the loan and the action UTxO) and the wallet address.

        Raises:
            ValueError: The capture is not one loan's collateral change and one
                market's redeem.
        """
        capture = CapturedTx.of(fix)
        collateral = CollateralSnapshot.from_capture(fix)
        redeem = RedeemSnapshot.from_capture(fix)
        spent = (collateral.loan.utxo, redeem.action.utxo)
        written = [
            *(a.utxo for a in capture.action_outputs(redeem.market)),
            *(loan.utxo for loan in capture.loan_outputs()),
        ]
        owner_address = capture.wallet_address(*spent, *written)
        funding = capture.funding(*spent)
        return cls(
            collateral=dataclasses.replace(
                collateral,
                owner_address=owner_address,
                funding=list(funding),
            ),
            redeem=dataclasses.replace(
                redeem,
                owner_address=owner_address,
                funding=list(funding),
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: str,
        owner_address: str,
        qtoken_unit: str,
        qtokens: int,
        action_utxo: str | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
        allow_spent: bool = False,
    ) -> RemoveAndRedeemSnapshot:
        """Resolve taking ``qtokens`` of ``qtoken_unit`` out of a loan and redeeming.

        The collateral leg is `CollateralSnapshot.from_backend` on the loan at
        ``loan_utxo`` with ``qtokens`` fewer of ``qtoken_unit``; the redeem leg
        burns them on the market whose qToken ``qtoken_unit`` is, paying ``floor(Q
        * s9)`` from the pinned ``action_utxo`` or a random one (by ``rng``) not in
        ``exclude``. The wallet UTxOs at ``owner_address`` fund both legs unless
        ``funding`` is given. The upper bound is ``valid_to_ms`` or the default
        window, capped at the earlier of the two markets' batch deadlines.
        ``allow_spent`` resolves an already consumed loan (a captured replay).

        Raises:
            ValueError: ``qtoken_unit`` is no Liqwid market's qToken, or the loan
                holds fewer than ``qtokens`` (or ``qtokens`` is not positive).
            UnsupportedLoanError: The loan is legacy-generation or no loan.
            MinValueError: The payout is below the redeemed market's minimum.
            MarketAwaitingBatchError: A market's batch deadline is too close.
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market
        from charli3_dendrite.lending.liqwid.transactions.resolve import (
            resolve_valid_to_ms,
        )

        active = resolve_deployment(deployment)
        market = (
            active.market_of_qtoken(qtoken_unit)
            if len(qtoken_unit) == POLICY_HEX_LEN
            else None
        )
        if market is None:
            msg = f"{qtoken_unit} is not a Liqwid qToken"
            raise ValueError(msg)
        loan = resolve_loan(
            backend,
            loan_utxo,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        held = loan.collateral.get(qtoken_unit, 0)
        if not 0 < qtokens <= held:
            msg = (
                f"loan {loan_utxo} holds {held} of {qtoken_unit}; cannot take out "
                f"{qtokens}"
            )
            raise ValueError(msg)
        target = dict(loan.collateral)
        target[qtoken_unit] = held - qtokens
        collateral = CollateralSnapshot.from_backend(
            backend,
            loan_utxo=loan_utxo,
            owner_address=owner_address,
            collateral=held_collateral(target),
            funding=funding,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        redeem = RedeemSnapshot._from_records(
            backend,
            resolve_market(backend, market, deployment=deployment),
            owner_address=owner_address,
            qtokens=qtokens,
            funding=collateral.funding,
            action_utxo=action_utxo,
            exclude=exclude,
            valid_to_ms=valid_to_ms,
            rng=rng,
            deployment=deployment,
        )
        loan_upper = resolve_valid_to_ms(backend, collateral.records, valid_to_ms)
        redeem.valid_to_ms = min(redeem.valid_to_ms, loan_upper)
        return cls(collateral=collateral, redeem=redeem)


# --- builds ------------------------------------------------------------------


def build_supply_and_borrow(
    tx_builder: TransactionBuilder,
    *,
    snapshot: SupplyAndBorrowSnapshot,
) -> None:
    """Add the supply-and-borrow of ``snapshot`` to ``tx_builder``.

    The funding is stashed on ``snapshot``. The caller balances (the borrowed
    amount and the change land in its change output), signs and submits.

    Raises:
        ValueError: The loan's collateral is not exactly the qTokens the supply
            mints, both legs spend one action UTxO, or the legs read different
            records of one market.
        LiqwidBuildError, ValidityWindowError: As `build_supply` and
            `build_borrow`.
    """
    _build_supply_and_borrow(tx_builder, snapshot, stash=snapshot)


def build_remove_and_redeem(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RemoveAndRedeemSnapshot,
) -> None:
    """Add the remove-and-redeem of ``snapshot`` to ``tx_builder``.

    The funding is stashed on ``snapshot``. The caller balances (the payout lands
    in its change output), signs and submits.

    Raises:
        ValueError: The collateral change does not take out exactly the qTokens
            the redeem burns, or the legs read different records of one market.
        LiqwidBuildError, ValidityWindowError: As `build_redeem` and
            `build_collateral`.
    """
    _build_remove_and_redeem(tx_builder, snapshot, stash=snapshot)


def _build_supply_and_borrow(
    tx_builder: TransactionBuilder,
    snapshot: SupplyAndBorrowSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_supply_and_borrow`, stashing the funding on ``stash``."""
    supply, borrow = snapshot.supply, snapshot.borrow
    minted = {supply.market.qtoken_policy: supply.qtokens}
    if held_collateral(borrow.collateral) != minted:
        msg = (
            f"the loan holds {borrow.collateral}, not exactly the qTokens the "
            f"supply mints {minted}"
        )
        raise ValueError(msg)
    if supply.action.out_ref == borrow.action.out_ref:
        msg = (
            f"the two legs spend two different action UTxOs, not "
            f"{supply.action.out_ref} twice"
        )
        raise ValueError(msg)
    _check_shared_records(supply.records, borrow.records, borrow.health)
    _build_supply(tx_builder, supply, stash=stash)
    _build_borrow(tx_builder, borrow, stash=stash)


def _build_remove_and_redeem(
    tx_builder: TransactionBuilder,
    snapshot: RemoveAndRedeemSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_remove_and_redeem`, stashing the funding on ``stash``."""
    collateral, redeem = snapshot.collateral, snapshot.redeem
    unit = redeem.market.qtoken_policy
    removed = collateral.loan.collateral.get(unit, 0) - collateral.collateral.get(
        unit,
        0,
    )
    if removed != redeem.qtokens:
        msg = (
            f"the collateral change removes {removed} of {unit} and the redeem "
            f"burns {redeem.qtokens}"
        )
        raise ValueError(msg)
    _check_shared_records(redeem.records, collateral.records, collateral.health)
    _build_redeem(tx_builder, redeem, stash=stash)
    _build_collateral(tx_builder, collateral, stash=stash)
    loan_market = collateral.records
    merge_validity_ms(
        tx_builder,
        upper_ms=ttl_cap_ms(loan_market.state_datum, loan_market.params_datum),
        lower_ms=None,
    )


def _check_shared_records(
    action_market: MarketRecords,
    loan_market: MarketRecords,
    health: HealthInputs | None,
) -> None:
    """Refuse two legs that read different records of one market.

    The action leg reads ``action_market``'s MarketParams and MarketState; the
    loan leg reads ``loan_market``'s and, through ``health``, the MarketState of
    each collateral market. A market both read must be read through one UTxO of
    each record, which the merged references then hold once.

    Raises:
        ValueError: The legs read two MarketParams or two MarketState UTxOs of one
            market.
    """
    market = action_market.market
    if market.market_id == loan_market.market.market_id:
        pairs = [
            ("MarketParams", action_market.params, loan_market.params),
            ("MarketState", action_market.state, loan_market.state),
        ]
    else:
        state = None if health is None else health.states.get(market.market_id)
        pairs = [] if state is None else [("MarketState", action_market.state, state)]
    for kind, mine, theirs in pairs:
        if mine.out_ref != theirs.out_ref:
            msg = (
                f"the legs read two {kind} records of market {market.name}: "
                f"{mine.out_ref} and {theirs.out_ref}"
            )
            raise ValueError(msg)
