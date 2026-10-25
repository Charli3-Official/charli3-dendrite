"""Liqwid V2 combinations: supply-and-borrow and remove-and-redeem.

Each combination is the exact union of two legs built by their own flows:

- **Supply-and-borrow** supplies ``A`` to market X (spending one of X's action
  UTxOs and minting ``Q = floor(A / s9)`` qX) and opens a loan on market Y (spending
  one of Y's action UTxOs) whose collateral is exactly those ``Q`` qX.
- **Remove-and-redeem** takes ``Q`` qX out of a loan on market Y (a collateral
  change) and redeems them on market X (spending one of X's action UTxOs), burning
  them straight out of the loan: no wallet input or output holds them.

X and Y are two different markets. One transaction spends at most one action UTxO
of a market: the Liqwid scripts refuse a supply-and-borrow whose legs spend two
action UTxOs of one market, and a remove-and-redeem whose redeem spends an action
UTxO of the loan's own market next to the loan's collateral change. Both
combinations refuse one market for X and Y with `UnsupportedMarketError`.

The action-only leg is added first and the loan leg last, so the loan flow's end
(health set and check, owner signer, borrow staking withdrawal, reference hygiene)
covers the whole transaction. The funding is spent and stashed once, on the
combination's snapshot; the references merge by out-ref (the first kept), so a
market both legs read is referenced once. The action leg's market, when it is a
collateral market of the loan, must be read through one MarketState by both legs.

The validity window ends by the batch deadline ``s8 + p[3]`` of each market whose
action UTxO the transaction spends: a supply-and-borrow's by both markets' (the
supply leg refuses a bound past its market's, the open caps its bound at the
borrow market's), a remove-and-redeem's by the redeemed market's alone. The
collateral change of a remove-and-redeem spends no action UTxO and its loan
validator checks no deadline, so the loan market's deadline does not bound it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import _build_borrow
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import _build_collateral
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import _build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import _build_supply
from charli3_dendrite.lending.liqwid.transactions.terms import held_collateral
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.units import POLICY_HEX_LEN

if TYPE_CHECKING:
    import random

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.deployment import MarketComponents
    from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
    from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "RemoveAndRedeemSnapshot",
    "SupplyAndBorrowSnapshot",
    "build_remove_and_redeem",
    "build_supply_and_borrow",
]


# A leg of a combination: each holds its own ``owner_address`` and ``funding``.
_Leg = TypeVar(
    "_Leg",
    SupplySnapshot,
    BorrowSnapshot,
    CollateralSnapshot,
    RedeemSnapshot,
)


@dataclass(frozen=True)
class _CapturedWallet:
    """The wallet address and inputs both legs of a captured combination share."""

    owner_address: str
    funding: tuple[Utxo, ...]

    @classmethod
    def of(
        cls,
        capture: CapturedTx,
        *,
        spent: Sequence[Utxo],
        written: Sequence[Utxo],
    ) -> _CapturedWallet:
        """The wallet of ``capture``: every input but the legs' ``spent`` UTxOs.

        The address is the first input's, else the first output's, outside
        ``spent`` and the protocol UTxOs ``written``.

        Raises:
            ValueError: Every input and output is one of the legs' UTxOs.
        """
        return cls(
            owner_address=capture.wallet_address(*spent, *written),
            funding=tuple(capture.funding(*spent)),
        )

    def share(self, leg: _Leg) -> _Leg:
        """``leg`` with this wallet's address and funding in place of its own."""
        return dataclasses.replace(
            leg,
            owner_address=self.owner_address,
            funding=list(self.funding),
        )


# Why a combination cannot act on one market (`UnsupportedMarketError` reasons).
_ONE_MARKET_SUPPLY_AND_BORROW = (
    "a supply-and-borrow needs two markets: the scripts refuse two action UTxOs "
    "of one market in one transaction"
)
_ONE_MARKET_REMOVE_AND_REDEEM = (
    "a remove-and-redeem needs a qToken of another market than the loan's: the "
    "scripts refuse the market's action UTxO next to a collateral change of its "
    "loan"
)


# --- snapshots ---------------------------------------------------------------


@dataclass
class SupplyAndBorrowSnapshot(PoolActionSnapshot):
    """A supply to one market and an open on another market.

    The legs carry the deployment and the datum style: a combination has no
    ``deployment`` or ``indefinite`` of its own, which could disagree with its
    legs', and nothing reads one from the combination.

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
        wallet = _CapturedWallet.of(capture, spent=spent, written=written)
        return cls(supply=wallet.share(supply), borrow=wallet.share(borrow))

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
        UTxOs. Both legs end at the earlier of their bounds.

        Raises:
            KeyError: No market has one of the names.
            UnsupportedMarketError: The two names are one market (refused before
                anything is read).
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        active = resolve_deployment(deployment)
        _refuse_one_market(
            active.market_by_name(supply_market),
            active.market_by_name(borrow_market),
            _ONE_MARKET_SUPPLY_AND_BORROW,
        )
        supply_pin, borrow_pin = action_utxos
        supply = SupplySnapshot.from_backend(
            backend,
            market=supply_market,
            owner_address=owner_address,
            amount=supply_amount,
            action_utxo=supply_pin,
            exclude=exclude,
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
            exclude=exclude,
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

    As for `SupplyAndBorrowSnapshot`, the deployment and the datum style are the
    legs'.

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
        wallet = _CapturedWallet.of(capture, spent=spent, written=written)
        return cls(collateral=wallet.share(collateral), redeem=wallet.share(redeem))

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
        burns them on the market whose qToken ``qtoken_unit`` is, which must not be
        the loan's market, paying ``floor(Q * s9)`` from the pinned ``action_utxo``
        or a random one (by ``rng``) not in ``exclude``. The wallet UTxOs at
        ``owner_address`` fund both legs unless ``funding`` is given. The upper
        bound is ``valid_to_ms`` or the default window, capped at the redeemed
        market's batch deadline; the loan market's deadline does not bound the
        transaction. ``allow_spent`` resolves an already consumed loan (a captured
        replay).

        Raises:
            ValueError: ``qtoken_unit`` is no Liqwid market's qToken, or the loan
                holds fewer than ``qtokens`` (or ``qtokens`` is not positive).
            UnsupportedLoanError: The loan is legacy-generation or no loan.
            UnsupportedMarketError: ``qtoken_unit`` is the qToken of the loan's
                market (refused once the loan is read, before anything else).
            MinValueError: The payout is below the redeemed market's minimum.
            MarketAwaitingBatchError: The redeemed market's batch deadline is too
                close.
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market

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
        _refuse_one_market(market, loan.market, _ONE_MARKET_REMOVE_AND_REDEEM)
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
        UnsupportedMarketError: Both legs act on one market.
        ValueError: The loan's collateral is not exactly the qTokens the supply
            mints, both legs spend one action UTxO, or the legs read different
            MarketState records of the supply market.
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
        UnsupportedMarketError: The redeemed qTokens are the loan market's own.
        ValueError: The collateral change does not take out exactly the qTokens
            the redeem burns, or the legs read different MarketState records of
            the redeemed market.
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
    _refuse_one_market(supply.market, borrow.market, _ONE_MARKET_SUPPLY_AND_BORROW)
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
    _check_shared_state(supply.records, borrow.health)
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
    _refuse_one_market(redeem.market, collateral.market, _ONE_MARKET_REMOVE_AND_REDEEM)
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
    _check_shared_state(redeem.records, collateral.health)
    _build_redeem(tx_builder, redeem, stash=stash)
    _build_collateral(tx_builder, collateral, stash=stash)


def _refuse_one_market(
    action_market: MarketComponents,
    loan_market: MarketComponents,
    reason: str,
) -> None:
    """Refuse a combination whose action leg and loan leg act on one market.

    Raises:
        UnsupportedMarketError: ``action_market`` is ``loan_market``.
    """
    if action_market.market_id == loan_market.market_id:
        raise UnsupportedMarketError(action_market.name, reason)


def _check_shared_state(
    action_market: MarketRecords,
    health: HealthInputs | None,
) -> None:
    """Refuse two legs that read different MarketState records of one market.

    The action leg reads ``action_market``'s MarketState; the loan leg reads,
    through ``health``, the MarketState of each collateral market other than its
    own. The action leg's market, a collateral market of the loan, must be read
    through one MarketState UTxO, which the merged references then hold once.

    Raises:
        ValueError: The legs read two MarketState UTxOs of the action leg's market.
    """
    market = action_market.market
    state = None if health is None else health.states.get(market.market_id)
    if state is not None and state.out_ref != action_market.state.out_ref:
        msg = (
            f"the legs read two MarketState records of market {market.name}: "
            f"{action_market.state.out_ref} and {state.out_ref}"
        )
        raise ValueError(msg)
