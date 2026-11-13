"""Liqwid V2 combinations of an action leg and a loan leg in one transaction.

Supply-and-borrow, supply-and-increase, supply-and-collateral, remove-and-redeem
and repay-and-redeem. Each combination is the exact union of legs built by their own
flows:

- **Supply-and-borrow** supplies ``A`` to market X (spending one of X's action
  UTxOs and minting ``Q = floor(A / s9)`` qX) and opens a loan on market Y (spending
  one of Y's action UTxOs) whose collateral is exactly those ``Q`` qX.
- **Supply-and-increase** supplies ``A`` to market X as a supply-and-borrow does
  and borrows more on an existing loan on market Y (spending one of Y's action
  UTxOs). The loan's collateral grows by exactly the ``Q`` qX minted, plus any
  qTokens the wallet already holds and posts beside them.
- **Supply-and-collateral** supplies ``A`` to market X as a supply-and-borrow does
  and changes the collateral of an existing loan on market Y (spending no action
  UTxO of Y). The loan's collateral grows by exactly the ``Q`` qX minted, plus any
  qTokens the wallet already holds and posts beside them.
- **Remove-and-redeem** takes ``Q`` qX out of a loan on market Y (a collateral
  change) and redeems them on market X (spending one of X's action UTxOs), burning
  them straight out of the loan: no wallet input or output holds them.
- **Repay-and-redeem** repays a loan on market Y (spending one of Y's action UTxOs,
  none on a zero-debt close) and redeems qTokens it releases on up to
  `MAX_REDEEMS` other markets X1, X2, ... (spending one action UTxO of each),
  burning each market's released qTokens straight out of the loan, as a
  remove-and-redeem does. A close releases the whole collateral, a partial repay
  what its target collateral drops.

X and Y are two different markets. One transaction spends at most one action UTxO
of a market: the Liqwid scripts refuse a supply-and-borrow or a supply-and-increase
whose legs spend two action UTxOs of one market, a supply-and-collateral that
supplies to the loan's own market, a remove-and-redeem whose redeem
spends an action UTxO of the loan's own market next to the loan's collateral
change, and a repay next to a redeem on the loan's market. The combinations refuse
one market for X and Y with `UnsupportedMarketError`.

A repay-and-redeem resolved without named units redeems what it can: it walks the
released units in order and returns to the wallet, as qTokens, each unit that is
no Liqwid qToken or the loan market's own, whose payout ``floor(Q * s9)`` is below
its market's minimum, that no single action UTxO of its market can pay out, or
that comes once the cap of redeems (`MAX_REDEEMS` by default) is resolved. A
redeemed market awaiting its batch, or with no action UTxO left to choose, is
refused, not returned.

The action-only leg is added first and the loan leg last, so the loan flow's end
(health set and check, owner signer, borrow staking withdrawal, reference hygiene)
covers the whole transaction. The funding is spent and stashed once, on the
combination's snapshot; the references merge by out-ref (the first kept), so a
market both legs read is referenced once. The action leg's market, when it is a
collateral market of the loan, must be read through one MarketState by both legs.

The validity window ends by the batch deadline ``s8 + p[3]`` of each market whose
action UTxO the transaction spends: a supply-and-borrow's and a
supply-and-increase's by both markets' (the supply leg refuses a bound past its
market's, the open and the borrow more cap their bound at the loan market's), a
supply-and-collateral's by the supply market's alone (its collateral change spends
no action UTxO and its loan validator checks no deadline), a remove-and-redeem's
by the redeemed market's alone, a repay-and-redeem's by the loan market's and every
redeemed market's (the earliest of them binds). The
collateral change of a remove-and-redeem and a zero-debt close spend no action
UTxO and their loan validator checks no deadline, so the loan market's deadline
does not bound them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from typing import TypeVar

from pycardano import TransactionBuilder

from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import _build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import _build_increase
from charli3_dendrite.lending.liqwid.transactions.capture import CapturedTx
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import _build_collateral
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MinValueError
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.repay import _build_repay
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
    "MAX_REDEEMS",
    "RemoveAndRedeemSnapshot",
    "RepayAndRedeemSnapshot",
    "SupplyAndBorrowSnapshot",
    "SupplyAndCollateralSnapshot",
    "SupplyAndIncreaseSnapshot",
    "build_remove_and_redeem",
    "build_repay_and_redeem",
    "build_supply_and_borrow",
    "build_supply_and_collateral",
    "build_supply_and_increase",
]

# The most markets a repay-and-redeem redeems on: each redeem adds an action spend,
# a qToken burn and a heavier borrow staking run, and three keep a close inside the
# transaction's execution-unit limit.
MAX_REDEEMS: Final = 3


# A leg of a combination: each holds its own ``owner_address`` and ``funding``.
_Leg = TypeVar(
    "_Leg",
    SupplySnapshot,
    BorrowSnapshot,
    IncreaseSnapshot,
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
_ONE_MARKET_SUPPLY_AND_INCREASE = (
    "a supply-and-increase needs two markets: the scripts refuse two action UTxOs "
    "of one market in one transaction"
)
_ONE_MARKET_SUPPLY_AND_COLLATERAL = (
    "a supply-and-collateral needs two markets: the scripts refuse the supplied "
    "market's action UTxO next to a collateral change of its loan"
)
_ONE_MARKET_REMOVE_AND_REDEEM = (
    "a remove-and-redeem needs a qToken of another market than the loan's: the "
    "scripts refuse the market's action UTxO next to a collateral change of its "
    "loan"
)
_ONE_MARKET_REPAY_AND_REDEEM = (
    "a repay-and-redeem needs a qToken of another market than the loan's: the "
    "scripts refuse a redeem on the loan's market next to a repay of its loan"
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
            collateral=_minted(supply),
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
class SupplyAndIncreaseSnapshot(PoolActionSnapshot):
    """A supply to one market and a borrow more on a loan of another market.

    As for `SupplyAndBorrowSnapshot`, the deployment and the datum style are the
    legs'.

    Attributes:
        supply: The supply leg; its qTokens join the loan's collateral.
        increase: The borrow more; its collateral target is the loan's collateral
            plus ``{qX: supply.qtokens}`` plus ``wallet_collateral``, per unit.
        wallet_collateral: qTokens the wallet holds and posts to the loan beside
            the minted ones (unit -> quantity, no zero quantities).
    """

    supply: SupplySnapshot
    increase: IncreaseSnapshot
    wallet_collateral: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_capture(cls, fix: dict[str, Any]) -> SupplyAndIncreaseSnapshot:
        """Rebuild the snapshot of a captured supply-and-increase (byte-exact replay).

        Each leg pins what its own ``from_capture`` pins (the action UTxO, amounts,
        output collateral, loan lovelace, signers, validity bounds and datum
        style); both legs share the wallet inputs (every input but the two action
        UTxOs and the loan) and the wallet address. The wallet collateral is what
        the loan gains beyond the qTokens the supply mints.

        Raises:
            ValueError: The capture is not a supply to one market and a borrow
                more on a loan of another, or the loan loses collateral beyond
                the minted qTokens.
        """
        capture = CapturedTx.of(fix)
        supply = SupplySnapshot.from_capture(fix)
        increase = IncreaseSnapshot.from_capture(fix)
        spent = (supply.action.utxo, increase.action.utxo, increase.loan.utxo)
        written = [
            *(a.utxo for a in capture.action_outputs(supply.market)),
            *(a.utxo for a in capture.action_outputs(increase.market)),
            *(loan.utxo for loan in capture.loan_outputs()),
        ]
        wallet = _CapturedWallet.of(capture, spent=spent, written=written)
        return cls(
            supply=wallet.share(supply),
            increase=wallet.share(increase),
            wallet_collateral=held_collateral(
                _wallet_added(
                    increase.loan.collateral,
                    increase.collateral_out,
                    minted=_minted(supply),
                ),
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: str,
        supply_market: str,
        owner_address: str,
        supply_amount: int,
        borrow_amount: int,
        wallet_collateral: Mapping[str, int] | None = None,
        action_utxos: tuple[str | None, str | None] = (None, None),
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
        allow_spent: bool = False,
    ) -> SupplyAndIncreaseSnapshot:
        """Resolve a supply of ``supply_amount`` and a borrow more of ``borrow_amount``.

        The supply leg is resolved as `SupplySnapshot.from_backend` on
        ``supply_market``; the borrow more as `IncreaseSnapshot.from_backend` on the
        loan at ``loan_utxo``, whose collateral target is the loan's collateral
        plus the qTokens the supply mints plus ``wallet_collateral`` (qTokens the
        wallet holds and posts too), summed per unit, funded by the same wallet
        UTxOs. ``action_utxos`` pins the supply's and the borrow more's action
        UTxOs. Both legs end at the earlier of their bounds. ``allow_spent``
        resolves an already consumed loan (a captured replay).

        Raises:
            ValueError: A ``wallet_collateral`` quantity is negative (before
                anything is read).
            KeyError: No market is named ``supply_market`` (before anything is
                read).
            UnsupportedLoanError: The loan is legacy-generation or no loan.
            UnsupportedMarketError: ``supply_market`` is the loan's market
                (refused once the loan is read, before anything else).
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        supply_pin, increase_pin = action_utxos
        supply, target, wallet = _supply_toward_loan(
            backend,
            loan_utxo=loan_utxo,
            supply_market=supply_market,
            owner_address=owner_address,
            supply_amount=supply_amount,
            wallet_collateral=wallet_collateral,
            supply_pin=supply_pin,
            exclude=exclude,
            valid_to_ms=valid_to_ms,
            funding=funding,
            rng=rng,
            deployment=deployment,
            allow_spent=allow_spent,
            reason=_ONE_MARKET_SUPPLY_AND_INCREASE,
        )
        increase = IncreaseSnapshot.from_backend(
            backend,
            loan_utxo=loan_utxo,
            owner_address=owner_address,
            amount=borrow_amount,
            collateral=target,
            action_utxo=increase_pin,
            exclude=exclude,
            valid_to_ms=valid_to_ms,
            funding=supply.funding,
            rng=rng,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        upper = min(supply.valid_to_ms, increase.valid_to_ms)
        supply.valid_to_ms = increase.valid_to_ms = upper
        return cls(supply=supply, increase=increase, wallet_collateral=wallet)


@dataclass
class SupplyAndCollateralSnapshot(PoolActionSnapshot):
    """A supply to one market and a collateral change on a loan of another market.

    As for `SupplyAndBorrowSnapshot`, the deployment and the datum style are the
    legs'.

    Attributes:
        supply: The supply leg; its qTokens join the loan's collateral.
        collateral: The collateral change; its target is the loan's collateral plus
            ``{qX: supply.qtokens}`` plus ``wallet_collateral``, per unit.
        wallet_collateral: qTokens the wallet holds and posts to the loan beside
            the minted ones (unit -> quantity, no zero quantities).
    """

    supply: SupplySnapshot
    collateral: CollateralSnapshot
    wallet_collateral: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_capture(cls, fix: dict[str, Any]) -> SupplyAndCollateralSnapshot:
        """Rebuild the snapshot of a captured supply-and-collateral (byte-exact replay).

        Each leg pins what its own ``from_capture`` pins (the action UTxO, amount,
        target collateral, loan lovelace, signers, validity bounds and datum
        style); both legs share the wallet inputs (every input but the action UTxO
        and the loan) and the wallet address. The wallet collateral is what the
        loan gains beyond the qTokens the supply mints.

        Raises:
            ValueError: The capture is not a supply to one market and a collateral
                change on a loan of another, or the loan loses collateral beyond
                the minted qTokens.
        """
        capture = CapturedTx.of(fix)
        supply = SupplySnapshot.from_capture(fix)
        collateral = CollateralSnapshot.from_capture(fix)
        spent = (supply.action.utxo, collateral.loan.utxo)
        written = [
            *(a.utxo for a in capture.action_outputs(supply.market)),
            *(loan.utxo for loan in capture.loan_outputs()),
        ]
        wallet = _CapturedWallet.of(capture, spent=spent, written=written)
        return cls(
            supply=wallet.share(supply),
            collateral=wallet.share(collateral),
            wallet_collateral=held_collateral(
                _wallet_added(
                    collateral.loan.collateral,
                    collateral.collateral,
                    minted=_minted(supply),
                ),
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: str,
        supply_market: str,
        owner_address: str,
        supply_amount: int,
        wallet_collateral: Mapping[str, int] | None = None,
        action_utxo: str | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
        allow_spent: bool = False,
    ) -> SupplyAndCollateralSnapshot:
        """Resolve a supply of ``supply_amount`` added to a loan's collateral.

        The supply leg is resolved as `SupplySnapshot.from_backend` on
        ``supply_market``; the collateral change as `CollateralSnapshot.from_backend`
        on the loan at ``loan_utxo``, whose target is the loan's collateral plus the
        qTokens the supply mints plus ``wallet_collateral`` (qTokens the wallet
        holds and posts too), summed per unit, funded by the same wallet UTxOs.
        ``action_utxo`` pins the supply's action UTxO. The transaction's upper bound
        is the supply leg's: the collateral change spends no action UTxO and checks
        no deadline. ``allow_spent`` resolves an already consumed loan (a captured
        replay).

        Raises:
            ValueError: A ``wallet_collateral`` quantity is negative (before
                anything is read).
            KeyError: No market is named ``supply_market`` (before anything is
                read).
            UnsupportedLoanError: The loan is legacy-generation or no loan.
            UnsupportedMarketError: ``supply_market`` is the loan's market
                (refused once the loan is read, before anything else).
            CollateralCountError: The target holds more than the loan market's
                ``p[14]`` units.
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        supply, target, wallet = _supply_toward_loan(
            backend,
            loan_utxo=loan_utxo,
            supply_market=supply_market,
            owner_address=owner_address,
            supply_amount=supply_amount,
            wallet_collateral=wallet_collateral,
            supply_pin=action_utxo,
            exclude=exclude,
            valid_to_ms=valid_to_ms,
            funding=funding,
            rng=rng,
            deployment=deployment,
            allow_spent=allow_spent,
            reason=_ONE_MARKET_SUPPLY_AND_COLLATERAL,
        )
        collateral = CollateralSnapshot.from_backend(
            backend,
            loan_utxo=loan_utxo,
            owner_address=owner_address,
            collateral=target,
            funding=supply.funding,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        return cls(supply=supply, collateral=collateral, wallet_collateral=wallet)


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
            NoActionUtxoError: The redeemed market has no action UTxO left to
                choose.
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market

        market = _qtoken_market(resolve_deployment(deployment), qtoken_unit)
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


@dataclass
class RepayAndRedeemSnapshot(PoolActionSnapshot):
    """A repay of a loan and redeems of the qTokens it releases, in one transaction.

    As for `SupplyAndBorrowSnapshot`, the deployment and the datum style are the
    legs'.

    Attributes:
        repay: The repay of the loan; a close releases its whole collateral, a
            partial repay what its target collateral drops.
        redeems: One redeem per market, none on the loan's, each burning exactly
            the qTokens of its market the repay releases; the wallet holds none of
            them. Empty, the combination is a plain repay.
    """

    repay: RepaySnapshot
    redeems: tuple[RedeemSnapshot, ...]

    @property
    def returned(self) -> dict[str, int]:
        """The released qTokens no redeem burns, by unit: they reach the wallet."""
        returned = dict(self.repay.released_collateral)
        for redeem in self.redeems:
            unit = redeem.market.qtoken_policy
            returned[unit] = returned.get(unit, 0) - redeem.qtokens
        return {unit: qty for unit, qty in returned.items() if qty}

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: str,
        owner_address: str,
        repay: int,
        collateral: Mapping[str, int] | None = None,
        redeem: Collection[str] | None = None,
        max_redeems: int = MAX_REDEEMS,
        action_utxos: Mapping[str, str] | None = None,
        exclude: Collection[str] = (),
        valid_to_ms: int | None = None,
        funding: Sequence[Utxo] | None = None,
        rng: random.Random | None = None,
        deployment: LiqwidDeployment | None = None,
        allow_spent: bool = False,
    ) -> RepayAndRedeemSnapshot:
        """Resolve a repay of ``repay`` on a loan and redeems of what it releases.

        The repay leg is `RepaySnapshot.from_backend` on the loan at ``loan_utxo``
        (``collateral`` is a partial repay's target). Each redeem leg burns every
        released qToken of its market, paying ``floor(Q * s9)``, funded by the
        repay's wallet UTxOs. ``redeem`` names the qToken units to redeem, each
        refused when it cannot be; without it every released unit is tried in
        order and returned to the wallet, as qTokens, when it is no Liqwid qToken
        or the loan market's own, its payout is below its market's minimum, no
        action UTxO of its market can pay it out, or ``max_redeems`` redeems are
        already resolved. A market with no action UTxO left to choose (none live,
        or every live one in ``exclude``) is refused with ``NoActionUtxoError``,
        never returned.
        ``action_utxos`` pins action UTxOs by market name (the loan's market's for
        the repay); the others are random choices (by ``rng``) not in ``exclude``.
        Every leg with a bound ends at the earliest of the legs' bounds
        (``valid_to_ms`` or the default window, each capped at its market's batch
        deadline). ``allow_spent`` resolves an already consumed loan (a captured
        replay).

        Raises:
            ValueError: A named unit is no Liqwid qToken, two are of one market,
                more than ``max_redeems`` are named (before anything is read), or
                a named unit is not released by the repay.
            UnsupportedLoanError: The loan is legacy-generation or no loan.
            UnsupportedMarketError: A named unit is the qToken of the loan's
                market (refused once the loan is read, before anything else).
            MinValueError: A named unit's payout is below its market's minimum.
            InsufficientLiquidityError: No action UTxO of a named unit's market
                holds its payout.
            MarketAwaitingBatchError: The loan market's or a redeemed market's
                batch deadline is too close.
            NoActionUtxoError: The loan market or a redeemed market has no action
                UTxO left to choose.
            LiqwidBuildError: As the legs' ``from_backend``.
        """
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
        from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market

        active = resolve_deployment(deployment)
        pins = dict(action_utxos or {})
        named = (
            None
            if redeem is None
            else _named_redeems(active, redeem, max_redeems=max_redeems)
        )
        # Read here to refuse a named redeem on the loan's market before any other
        # read; the repay leg's ``from_backend`` reads the loan again.
        loan = resolve_loan(
            backend,
            loan_utxo,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        for market in (named or {}).values():
            _refuse_one_market(market, loan.market, _ONE_MARKET_REPAY_AND_REDEEM)
        repay_leg = RepaySnapshot.from_backend(
            backend,
            loan_utxo=loan_utxo,
            owner_address=owner_address,
            repay=repay,
            collateral=collateral,
            action_utxo=pins.get(loan.market.name),
            exclude=exclude,
            valid_to_ms=valid_to_ms,
            funding=funding,
            rng=rng,
            deployment=deployment,
            allow_spent=allow_spent,
        )
        released = repay_leg.released_collateral

        def redeem_leg(unit: str, market: MarketComponents) -> RedeemSnapshot:
            return RedeemSnapshot._from_records(
                backend,
                resolve_market(backend, market, deployment=deployment),
                owner_address=owner_address,
                qtokens=released[unit],
                funding=repay_leg.funding,
                action_utxo=pins.get(market.name),
                exclude=exclude,
                valid_to_ms=valid_to_ms,
                rng=rng,
                deployment=deployment,
            )

        redeems: list[RedeemSnapshot] = []
        if named is not None:
            for unit in named:
                if unit not in released:
                    msg = f"the repay of loan {loan_utxo} does not release {unit}"
                    raise ValueError(msg)
            redeems = [redeem_leg(unit, market) for unit, market in named.items()]
        else:
            for unit in sorted(released):
                if len(redeems) >= max_redeems:
                    break
                market = _qtoken_market(active, unit)
                if market is None or market.market_id == repay_leg.market.market_id:
                    continue
                try:
                    redeems.append(redeem_leg(unit, market))
                except (MinValueError, InsufficientLiquidityError):
                    continue
        _end_together(repay_leg, *redeems)
        return cls(repay=repay_leg, redeems=tuple(redeems))


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


def build_supply_and_increase(
    tx_builder: TransactionBuilder,
    *,
    snapshot: SupplyAndIncreaseSnapshot,
) -> None:
    """Add the supply-and-increase of ``snapshot`` to ``tx_builder``.

    The funding is stashed on ``snapshot``. The caller balances (the borrowed
    amount and the change land in its change output), signs and submits.

    Raises:
        UnsupportedMarketError: Both legs act on one market.
        ValueError: The loan's collateral does not grow by exactly the qTokens the
            supply mints plus ``wallet_collateral``, both legs spend one action
            UTxO, or the legs read different MarketState records of the supply
            market.
        LiqwidBuildError, ValidityWindowError: As `build_supply` and
            `build_increase`.
    """
    _build_supply_and_increase(tx_builder, snapshot, stash=snapshot)


def build_supply_and_collateral(
    tx_builder: TransactionBuilder,
    *,
    snapshot: SupplyAndCollateralSnapshot,
) -> None:
    """Add the supply-and-collateral of ``snapshot`` to ``tx_builder``.

    The funding is stashed on ``snapshot``. The caller balances (the change lands in
    its change output), signs and submits.

    Raises:
        UnsupportedMarketError: Both legs act on one market.
        ValueError: The loan's collateral does not grow by exactly the qTokens the
            supply mints plus ``wallet_collateral``, or the legs read different
            MarketState records of the supply market.
        CollateralCountError: The target holds more than ``p[14]`` units.
        LiqwidBuildError, ValidityWindowError: As `build_supply` and
            `build_collateral`.
    """
    _build_supply_and_collateral(tx_builder, snapshot, stash=snapshot)


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


def build_repay_and_redeem(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RepayAndRedeemSnapshot,
) -> None:
    """Add the repay-and-redeem of ``snapshot`` to ``tx_builder``.

    The funding is stashed on ``snapshot``. The caller balances (the payouts, the
    returned qTokens and the loan lovelace land in its change output), signs and
    submits.

    Raises:
        UnsupportedMarketError: A redeem is on the loan's market.
        ValueError: Two redeems are on one market or spend the repay's action
            UTxO, a redeem does not burn exactly the qTokens of its market the
            repay releases, or a redeem and the repay read different MarketState
            records of the redeemed market.
        LiqwidBuildError, ValidityWindowError: As `build_redeem` and
            `build_repay`.
    """
    _build_repay_and_redeem(tx_builder, snapshot, stash=snapshot)


def _build_supply_and_borrow(
    tx_builder: TransactionBuilder,
    snapshot: SupplyAndBorrowSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_supply_and_borrow`, stashing the funding on ``stash``."""
    supply, borrow = snapshot.supply, snapshot.borrow
    _refuse_one_market(supply.market, borrow.market, _ONE_MARKET_SUPPLY_AND_BORROW)
    minted = _minted(supply)
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


def _build_supply_and_increase(
    tx_builder: TransactionBuilder,
    snapshot: SupplyAndIncreaseSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_supply_and_increase`, stashing the funding on ``stash``."""
    supply, increase = snapshot.supply, snapshot.increase
    _refuse_one_market(supply.market, increase.market, _ONE_MARKET_SUPPLY_AND_INCREASE)
    _check_added(
        increase.loan.collateral,
        increase.collateral_out,
        minted=_minted(supply),
        wallet=snapshot.wallet_collateral,
    )
    if supply.action.out_ref == increase.action.out_ref:
        msg = (
            f"the two legs spend two different action UTxOs, not "
            f"{supply.action.out_ref} twice"
        )
        raise ValueError(msg)
    _check_shared_state(supply.records, increase.health)
    _build_supply(tx_builder, supply, stash=stash)
    _build_increase(tx_builder, increase, stash=stash)


def _build_supply_and_collateral(
    tx_builder: TransactionBuilder,
    snapshot: SupplyAndCollateralSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_supply_and_collateral`, stashing the funding on ``stash``."""
    supply, collateral = snapshot.supply, snapshot.collateral
    _refuse_one_market(
        supply.market,
        collateral.market,
        _ONE_MARKET_SUPPLY_AND_COLLATERAL,
    )
    _check_added(
        collateral.loan.collateral,
        collateral.collateral,
        minted=_minted(supply),
        wallet=snapshot.wallet_collateral,
    )
    _check_shared_state(supply.records, collateral.health)
    _build_supply(tx_builder, supply, stash=stash)
    _build_collateral(tx_builder, collateral, stash=stash)


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


def _build_repay_and_redeem(
    tx_builder: TransactionBuilder,
    snapshot: RepayAndRedeemSnapshot,
    *,
    stash: PoolActionSnapshot,
) -> None:
    """`build_repay_and_redeem`, stashing the funding on ``stash``."""
    repay, redeems = snapshot.repay, snapshot.redeems
    released = repay.released_collateral
    repay_action = None if repay.action is None else repay.action.out_ref
    markets: set[str] = set()
    for redeem in redeems:
        _refuse_one_market(redeem.market, repay.market, _ONE_MARKET_REPAY_AND_REDEEM)
        if redeem.market.market_id in markets:
            msg = f"two redeems on market {redeem.market.name}"
            raise ValueError(msg)
        markets.add(redeem.market.market_id)
        if redeem.action.out_ref == repay_action:
            msg = (
                f"the repay and a redeem spend two different action UTxOs, not "
                f"{repay_action} twice"
            )
            raise ValueError(msg)
        unit = redeem.market.qtoken_policy
        if released.get(unit, 0) != redeem.qtokens:
            msg = (
                f"the repay releases {released.get(unit, 0)} of {unit} and the "
                f"redeem burns {redeem.qtokens}"
            )
            raise ValueError(msg)
        _check_shared_state(redeem.records, repay.health)
    for redeem in redeems:
        _build_redeem(tx_builder, redeem, stash=stash)
    _build_repay(tx_builder, repay, stash=stash)


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


def _supply_toward_loan(
    backend: AbstractBackend,
    *,
    loan_utxo: str,
    supply_market: str,
    owner_address: str,
    supply_amount: int,
    wallet_collateral: Mapping[str, int] | None,
    supply_pin: str | None,
    exclude: Collection[str],
    valid_to_ms: int | None,
    funding: Sequence[Utxo] | None,
    rng: random.Random | None,
    deployment: LiqwidDeployment | None,
    allow_spent: bool,
    reason: str,
) -> tuple[SupplySnapshot, dict[str, int], dict[str, int]]:
    """The supply leg of a combination that adds its qTokens to an existing loan.

    Checks ``wallet_collateral`` and names ``supply_market`` before anything is
    read, reads the loan at ``loan_utxo``, refuses a ``supply_market`` that is the
    loan's market with ``reason``, then resolves the supply as
    `SupplySnapshot.from_backend` (``supply_pin`` pins its action UTxO).

    Returns:
        The supply leg; the loan's collateral target, its collateral plus the
        qTokens the supply mints plus ``wallet_collateral``, summed per unit; and
        ``wallet_collateral`` without its zero quantities.

    Raises:
        ValueError: A ``wallet_collateral`` quantity is negative.
        KeyError: No market is named ``supply_market``.
        UnsupportedLoanError: The loan is legacy-generation or no loan.
        UnsupportedMarketError: ``supply_market`` is the loan's market.
        LiqwidBuildError: As `SupplySnapshot.from_backend`.
    """
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan

    wallet = held_collateral(wallet_collateral or {})
    market = resolve_deployment(deployment).market_by_name(supply_market)
    loan = resolve_loan(
        backend,
        loan_utxo,
        deployment=deployment,
        allow_spent=allow_spent,
    )
    _refuse_one_market(market, loan.market, reason)
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
    target = _sum_units(loan.collateral, _minted(supply), wallet)
    return supply, target, wallet


def _minted(supply: SupplySnapshot) -> dict[str, int]:
    """The qTokens ``supply`` mints, as collateral (unit -> quantity)."""
    return {supply.market.qtoken_policy: supply.qtokens}


def _sum_units(*quantities: Mapping[str, int]) -> dict[str, int]:
    """``quantities`` summed per unit, without the units that sum to zero."""
    total: dict[str, int] = {}
    for each in quantities:
        for unit, qty in each.items():
            total[unit] = total.get(unit, 0) + qty
    return {unit: qty for unit, qty in total.items() if qty}


def _check_added(
    loan_collateral: Mapping[str, int],
    target: Mapping[str, int],
    *,
    minted: Mapping[str, int],
    wallet: Mapping[str, int],
) -> None:
    """Refuse a target collateral that does not add exactly ``minted + wallet``.

    Raises:
        ValueError: ``target - loan_collateral`` differs from ``minted + wallet``
            in some unit (a missing unit counts as 0).
    """
    beyond = _wallet_added(loan_collateral, target, minted=minted)
    posted = _sum_units(wallet)
    if beyond != posted:
        msg = (
            f"the target adds {beyond} to the loan's collateral beyond the qTokens "
            f"the supply mints {dict(minted)}, not exactly the wallet's {posted}"
        )
        raise ValueError(msg)


def _wallet_added(
    loan_collateral: Mapping[str, int],
    target: Mapping[str, int],
    *,
    minted: Mapping[str, int],
) -> dict[str, int]:
    """What ``target`` adds to ``loan_collateral`` beyond the ``minted`` qTokens.

    The qTokens the wallet posts to the loan beside the minted ones, per unit
    without the zero quantities (a missing unit counts as 0); a negative quantity
    is collateral the target drops.
    """
    return _sum_units(target, _negated(loan_collateral), _negated(minted))


def _negated(quantities: Mapping[str, int]) -> dict[str, int]:
    """``quantities`` with every quantity negated."""
    return {unit: -qty for unit, qty in quantities.items()}


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


def _end_together(*legs: RepaySnapshot | RedeemSnapshot) -> None:
    """End every leg with an upper bound at the earliest of their bounds.

    A zero-debt close has no bound of its own and is given none: it spends no
    action UTxO, so only the other legs' bounds limit the transaction.
    """
    bounds = [leg.valid_to_ms for leg in legs if leg.valid_to_ms is not None]
    if not bounds:
        return
    upper = min(bounds)
    for leg in legs:
        if leg.valid_to_ms is not None:
            leg.valid_to_ms = upper


def _qtoken_market(
    deployment: LiqwidDeployment,
    unit: str,
) -> MarketComponents | None:
    """The market whose qToken is ``unit`` (a bare 56-hex policy), or None."""
    if len(unit) != POLICY_HEX_LEN:
        return None
    return deployment.market_of_qtoken(unit)


def _named_redeems(
    deployment: LiqwidDeployment,
    units: Collection[str],
    *,
    max_redeems: int,
) -> dict[str, MarketComponents]:
    """The market of each of ``units``, the qTokens a repay-and-redeem redeems.

    Raises:
        ValueError: A unit is no Liqwid qToken, two are of one market, or there
            are more than ``max_redeems``.
    """
    markets: dict[str, MarketComponents] = {}
    for unit in units:
        market = _qtoken_market(deployment, unit)
        if market is None:
            msg = f"{unit} is not a Liqwid qToken"
            raise ValueError(msg)
        if any(m.market_id == market.market_id for m in markets.values()):
            msg = f"one redeem per market: {unit} is named twice"
            raise ValueError(msg)
        markets[unit] = market
    if len(markets) > max_redeems:
        msg = (
            f"a repay-and-redeem redeems on at most {max_redeems} markets, not "
            f"{len(markets)}"
        )
        raise ValueError(msg)
    return markets
