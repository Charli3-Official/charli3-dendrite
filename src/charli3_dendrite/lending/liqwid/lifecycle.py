"""Liqwid V2 loan lifecycle values.

A Liqwid loan has no identity token: every change re-creates the loan UTxO under a
new out-ref, and the loan token it holds is shared by every loan of its market. A
loan's history is therefore a chain of out-refs, each spent loan matched to the loan
its transaction re-created. This module holds what that matching compares
(:class:`LoanKey`), the evidence a consuming transaction carries (:class:`LoanSpend`)
and what a spend did to the loan (:class:`LoanTransition`).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


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
