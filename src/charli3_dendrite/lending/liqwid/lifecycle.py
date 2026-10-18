"""Liqwid V2 loan lifecycle: successor matching and spend classification.

A Liqwid loan carries no unique token: a modification, a liquidation or an admin
migration spends the loan UTxO and may re-create it at a loan validator.
:func:`loan_key` reduces any output at a loan validator to the fields the matcher
compares, :func:`match_successors` links the loans one transaction spent to the loans
it created, :func:`loan_spend` gathers the transaction's withdrawals and mints, and
:func:`classify_spend` names what happened to each spent loan. Everything is pure: the
caller reads the transaction, these functions only compare.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from collections.abc import Hashable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from operator import attrgetter
from typing import TYPE_CHECKING

from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.units import canonical_unit_key
from charli3_dendrite.lending.units import out_ref
from charli3_dendrite.lending.units import policy_of
from charli3_dendrite.lending.units import script_credential

if TYPE_CHECKING:
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
    from charli3_dendrite.lending.liqwid.deployment import MarketComponents


@dataclass(frozen=True)
class LoanKey:
    """A loan UTxO as successor matching sees it.

    Attributes:
        out_ref: The loan UTxO, ``"<tx_hash>#<index>"``.
        market_id: The market whose loan validator holds the UTxO.
        owner: The borrower's key hash; None for an output without a loan datum.
        principal: Principal owed (loan datum field 1).
        interest: Interest accrued up to ``loan_index`` (loan datum field 2).
        min_interest: The least interest the loan owes in total (loan datum field 3).
        loan_index: The market interest index the loan was last settled at (field 4).
        collateral: ``(unit, quantity)`` per held collateral unit, sorted by unit.
        generation: The loan validator generation, ``"legacy"`` or ``"new"``.
    """

    out_ref: str
    market_id: str
    owner: str | None
    principal: int
    interest: int
    min_interest: int
    loan_index: int
    collateral: tuple[tuple[str, int], ...]
    generation: str


@dataclass(frozen=True)
class LoanSpend:
    """What the transaction that spent a loan did besides spending it.

    Attributes:
        withdrawal_scripts: Stake script hashes the transaction withdrew from.
        minted: Net minted quantity per policy (negative for a net burn); compared
            by equality, left out of the hash.
        burned_any: Policies with any negative mint entry, so a burn that a mint in
            the same transaction cancels (a token flip, net 0) is still seen.
    """

    withdrawal_scripts: frozenset[str]
    minted: Mapping[str, int] = field(hash=False)
    burned_any: frozenset[str]


class LoanTransition(str, Enum):
    """What a spend did to a loan."""

    MODIFIED = "modified"  # borrowed more, repaid part, or changed collateral
    REPAID = "repaid"  # closed by its owner
    LIQUIDATED = "liquidated"  # closed by a liquidation
    PARTIALLY_LIQUIDATED = "partially_liquidated"  # liquidated, a successor remains
    MIGRATED = "migrated"  # re-created by an administrator, possibly re-owned
    PARKED = "parked"  # spent with no successor and no close evidence (parking)


_State = tuple[int, int, int, int, tuple[tuple[str, int], ...]]


def collateral_units(
    info: PoolStateInfo,
    loan_token_policies: frozenset[str],
) -> tuple[tuple[str, int], ...]:
    """A loan UTxO's collateral: ``(unit, qty)`` of every held unit, in unit order.

    Lovelace and units under ``loan_token_policies`` (the loan tokens, whose ``00`` /
    ``01`` name flips on a modification) are not collateral. A qToken holding is its
    bare 56-hex policy; a whole-policy collateral keeps its full policy and name.
    """
    return tuple(
        sorted(
            (
                (unit, quantity)
                for unit, quantity in info.assets.items()
                if unit != "lovelace" and policy_of(unit) not in loan_token_policies
            ),
            key=lambda held: canonical_unit_key(held[0]),
        ),
    )


def loan_key(
    info: PoolStateInfo,
    deployment: LiqwidDeployment | None = None,
) -> LoanKey | None:
    """The matcher's view of any UTxO at a Liqwid loan validator; None elsewhere.

    The market and the validator generation come from the payment credential alone,
    so an output that is not a genuine loan (a ``C0[]`` parking output, a datum-less
    UTxO, a datum that does not decode) still gets a key, with ``owner`` None and
    zero amounts; :func:`match_successors` never links such a key. ``collateral`` is
    every held unit except lovelace and the loan tokens (whose ``00`` / ``01`` name
    flips on a modification), sorted by unit. Never raises.

    Args:
        info: The UTxO record.
        deployment: The deployment to read validators from; the bundled one if None.

    Returns:
        The key, or None when the payment credential is not a Liqwid loan validator.
    """
    active = resolve_deployment(deployment)
    credential = script_credential(info.address)
    if credential is None:
        return None
    found = active.market_of_loan_validator(credential)
    if found is None:
        return None
    market, generation = found
    record = parse_utxo(info, active)
    datum = record.datum if record is not None else None
    loan = datum if isinstance(datum, LoanDatum) else None
    return LoanKey(
        out_ref=out_ref(info),
        market_id=market.market_id,
        owner=loan.owner if loan is not None else None,
        principal=loan.principal if loan is not None else 0,
        interest=loan.interest if loan is not None else 0,
        min_interest=loan.min_interest if loan is not None else 0,
        loan_index=loan.index if loan is not None else 0,
        collateral=collateral_units(info, active.loan_token_policies),
        generation=generation,
    )


def _state(key: LoanKey) -> _State:
    """The fields the second matcher pass compares."""
    return (
        key.principal,
        key.interest,
        key.min_interest,
        key.loan_index,
        key.collateral,
    )


def _distinct(keys: Iterable[LoanKey]) -> list[LoanKey]:
    """``keys`` without repeated out-refs, first occurrence kept."""
    seen: dict[str, LoanKey] = {}
    for key in keys:
        seen.setdefault(key.out_ref, key)
    return list(seen.values())


def _unique_links(
    spent: Sequence[LoanKey],
    created: Sequence[LoanKey],
    by: Callable[[LoanKey], Hashable],
) -> dict[str, str]:
    """Spent to created out-ref wherever ``by`` is shared by exactly one of each."""
    spent_counts = Counter(by(key) for key in spent)
    created_groups: dict[Hashable, list[LoanKey]] = {}
    for key in created:
        created_groups.setdefault(by(key), []).append(key)
    links: dict[str, str] = {}
    for key in spent:
        value = by(key)
        group = created_groups.get(value, [])
        if spent_counts[value] == 1 and len(group) == 1:
            links[key.out_ref] = group[0].out_ref
    return links


def match_successors(
    spent: Sequence[LoanKey],
    created: Sequence[LoanKey],
) -> dict[str, str]:
    """Link the loans one transaction spent to the loans it created, per market.

    Pass 1 links equal owners where the owner is unique among the market's spent
    loans and among its created loans. Pass 2, over what is still unlinked, links
    equal ``(principal, interest, min_interest, loan_index, collateral)`` unique on
    both sides: it resolves several loans of one owner and an admin re-own. A key
    whose ``owner`` is None (not a genuine loan) is never linked, so spending a loan
    into a parking output leaves it without a successor. A loan never links across
    markets.

    Args:
        spent: Keys of the transaction's inputs at loan validators.
        created: Keys of the transaction's outputs at loan validators.

    Returns:
        Spent out-ref to created out-ref; an unlinked spent loan is absent.
    """
    links: dict[str, str] = {}
    for market_id in sorted({key.market_id for key in spent}):
        left = _distinct(
            k for k in spent if k.market_id == market_id and k.owner is not None
        )
        right = _distinct(
            k for k in created if k.market_id == market_id and k.owner is not None
        )
        by_owner = _unique_links(left, right, attrgetter("owner"))
        taken = set(by_owner.values())
        by_state = _unique_links(
            [k for k in left if k.out_ref not in by_owner],
            [k for k in right if k.out_ref not in taken],
            _state,
        )
        links.update(by_owner)
        links.update(by_state)
    return links


def loan_spend(
    withdrawals: Iterable[str],
    mints: Iterable[tuple[str, int]],
) -> LoanSpend:
    """The evidence of one consuming transaction, from its raw rows.

    Args:
        withdrawals: Stake script hashes (hex) the transaction withdrew from.
        mints: One ``(policy, quantity)`` row per minted or burned asset, a burn
            negative. A row may name the full unit (policy id followed by asset
            name); only its leading 56 hex characters, the policy id, count.

    Returns:
        ``minted`` holds the net quantity per policy, zero nets kept, so a loan-token
        flip that burns the ``00`` name and mints the ``01`` name nets 0;
        ``burned_any`` holds every policy with a negative row, so that flip still
        shows as a burn.
    """
    net: dict[str, int] = {}
    burned: set[str] = set()
    for unit, quantity in mints:
        policy = policy_of(unit)
        net[policy] = net.get(policy, 0) + quantity
        if quantity < 0:
            burned.add(policy)
    return LoanSpend(
        withdrawal_scripts=frozenset(withdrawals),
        minted=net,
        burned_any=frozenset(burned),
    )


def _market(deployment: LiqwidDeployment, market_id: str) -> MarketComponents | None:
    """The market, or None for an id the deployment does not know."""
    try:
        return deployment.market(market_id)
    except KeyError:
        return None


def _liquidation(continued: bool) -> LoanTransition:
    """A liquidation that left a successor is partial."""
    if continued:
        return LoanTransition.PARTIALLY_LIQUIDATED
    return LoanTransition.LIQUIDATED


def _by_evidence(
    market: MarketComponents,
    continued: bool,
    spend: LoanSpend,
) -> LoanTransition | None:
    """Rules 1-5: the market's withdrawals first, then its mints and burns."""
    withdrawn = spend.withdrawal_scripts
    if withdrawn.intersection(market.liquidation_staking):
        return _liquidation(continued)
    if withdrawn.intersection(market.borrow_staking):
        return LoanTransition.MODIFIED if continued else LoanTransition.REPAID
    liquidation = market.liquidation_policy
    if liquidation is not None and spend.minted.get(liquidation, 0) > 0:
        return _liquidation(continued)
    burned = {
        policy
        for policy in market.loan_token_policies
        if policy in spend.burned_any or spend.minted.get(policy, 0) < 0
    }
    if burned and not continued:
        return LoanTransition.REPAID
    if continued and any(spend.minted.get(policy, 0) >= 0 for policy in burned):
        return LoanTransition.MODIFIED
    return None


def classify_spend(
    spent: LoanKey,
    successor: LoanKey | None,
    spend: LoanSpend,
    deployment: LiqwidDeployment | None = None,
) -> LoanTransition:
    """What the consuming transaction did to one spent loan; first matching rule wins.

    The evidence counts only for the spent loan's own market, across every
    generation of its validators and policies:

    1. a withdrawal from its LiquidationStaking: ``PARTIALLY_LIQUIDATED`` with a
       successor, ``LIQUIDATED`` without;
    2. a withdrawal from its BorrowStaking: ``MODIFIED`` / ``REPAID``;
    3. its Liquidation policy minted: ``PARTIALLY_LIQUIDATED`` / ``LIQUIDATED``;
    4. its Borrow or NewBorrow policy burned, no successor: ``REPAID``;
    5. a loan-token flip (burned and minted again) with a successor: ``MODIFIED``;
       the flip is read as a burned policy whose net mint is zero or more;
    6. none of the above, a successor at another generation or with another owner:
       ``MIGRATED``;
    7. none of the above, no successor (a parking output): ``PARKED``;
    8. none of the above, a successor at the same generation with the same owner
       (an in-place rewrite): ``MIGRATED``.

    Rules 7 and 8 cover every remaining case, so this never raises; a market id the
    deployment does not know skips rules 1-5.

    The evidence is per transaction, not per loan. A transaction that fully repays
    one loan (a burn) and flips another of the same market and policy nets that
    policy below zero, so the flipped loan fails rule 5 and is labelled
    ``MIGRATED`` by rule 8. ``LoanSpend`` keeps no positive-mint evidence to tell
    that case from a successor whose loan token was only burned. The gated live
    test (``tests/lending/liqwid/test_market_live.py``) checks that the chain's
    history holds no such transaction.

    Args:
        spent: The spent loan.
        successor: Its successor from :func:`match_successors`, or None.
        spend: The consuming transaction's evidence (:func:`loan_spend`).
        deployment: The deployment to read hashes from; the bundled one if None.

    Returns:
        The transition.
    """
    active = resolve_deployment(deployment)
    market = _market(active, spent.market_id)
    continued = successor is not None
    if market is not None:
        transition = _by_evidence(market, continued, spend)
        if transition is not None:
            return transition
    # Rules 6 and 8 (a migration elsewhere, or an in-place rewrite) both migrate.
    return LoanTransition.MIGRATED if continued else LoanTransition.PARKED


class LiqwidLifecycle:
    """Liqwid V2 chained loans, bound to one deployment (the bundled one by default).

    Implements :class:`~charli3_dendrite.lending.markets.PositionLifecycle`: an
    indexer chains loan UTxOs through :meth:`match_successors` and labels each spent
    loan with :meth:`classify_spend`. ``loan_token_policies`` are the Borrow and
    NewBorrow policies of every market; their ``00`` / ``01`` units are shared by
    many loans and identify none.
    """

    def __init__(self, deployment: LiqwidDeployment | None = None) -> None:
        """Bind ``deployment``, or the bundled deployment when None."""
        self.deployment: LiqwidDeployment = resolve_deployment(deployment)
        self.loan_token_policies: frozenset[str] = self.deployment.loan_token_policies

    def loan_key(self, info: PoolStateInfo) -> LoanKey | None:
        """:func:`loan_key` against this deployment."""
        return loan_key(info, self.deployment)

    def match_successors(
        self,
        spent: Sequence[LoanKey],
        created: Sequence[LoanKey],
    ) -> dict[str, str]:
        """:func:`match_successors`."""
        return match_successors(spent, created)

    def classify_spend(
        self,
        spent: LoanKey,
        successor: LoanKey | None,
        spend: LoanSpend,
    ) -> LoanTransition:
        """:func:`classify_spend` against this deployment."""
        return classify_spend(spent, successor, spend, self.deployment)
