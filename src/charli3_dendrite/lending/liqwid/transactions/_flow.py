"""Steps the Liqwid V2 flow builders share around their legs.

The supply, redeem, borrow, repay and collateral builders each check the flow's
limits on the action UTxO they spend, reference the market's MarketParams and
MarketState, keep a modified loan's lovelace and close with a validity window in
POSIX milliseconds. Those steps live here once; the legs themselves are in
:mod:`~charli3_dendrite.lending.liqwid.transactions.legs`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from charli3_dendrite.lending.liqwid.transactions.terms import check_limits
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.validity import merge_validity
from charli3_dendrite.utility import posix_ms_to_slot

if TYPE_CHECKING:
    from pycardano import TransactionBuilder

    from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
    from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
    from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
    from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
    from charli3_dendrite.lending.liqwid.transactions.terms import Flow
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "check_flow_limits",
    "kept_loan_lovelace",
    "market_references",
    "merge_validity_ms",
]


def check_flow_limits(
    flow: Flow,
    *,
    records: MarketRecords,
    action: ActionUtxo,
    deltas: ActionDeltas,
    amount: int,
    output_collateral_count: int,
) -> None:
    """Check ``flow``'s limits (`check_limits`) on the action UTxO it spends.

    ``records`` are the market's, ``deltas`` the change to ``action``'s record and
    ``amount`` what the market minimum ``p[1]`` is checked on.

    Raises:
        LiqwidBuildError: A market-level or per-UTxO limit fails.
    """
    underlying = records.market.underlying
    check_limits(
        flow,
        params=records.params_datum,
        state=records.state_datum,
        action_datum=action.datum,
        deltas=deltas,
        amount=amount,
        output_collateral_count=output_collateral_count,
        action_underlying=action.underlying_amount(underlying),
        underlying=underlying,
    )


def market_references(records: MarketRecords) -> list[Utxo]:
    """The market's MarketParams and MarketState UTxOs, which its scripts read."""
    return [utxo_from_info(records.params.info), utxo_from_info(records.state.info)]


def kept_loan_lovelace(loan: LoanRecord, pinned: int | None) -> int:
    """The least lovelace a modified loan's output keeps.

    ``pinned`` (a replayed capture's) when given, else the input loan's lovelace.
    """
    return loan.utxo.lovelace if pinned is None else pinned


def merge_validity_ms(
    tx_builder: TransactionBuilder,
    *,
    upper_ms: int,
    lower_ms: int | None,
) -> None:
    """Intersect the builder's validity window with bounds in POSIX milliseconds.

    Raises:
        ValidityWindowError: The merged window is empty.
    """
    merge_validity(
        tx_builder,
        upper_slot=posix_ms_to_slot(upper_ms),
        lower_slot=None if lower_ms is None else posix_ms_to_slot(lower_ms),
    )
