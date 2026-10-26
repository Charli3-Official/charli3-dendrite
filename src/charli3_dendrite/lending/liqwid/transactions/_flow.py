"""Steps the Liqwid V2 flow builders share around their legs.

The supply, redeem, borrow, repay and collateral builders each check the flow's
limits on the action UTxO they spend, require the reference scripts they run,
reference the market's MarketParams and MarketState, write a loan at the market's
loan validator, keep a modified loan's lovelace and close with a validity window
in POSIX milliseconds. Those steps live here once; the legs themselves are in
:mod:`~charli3_dendrite.lending.liqwid.transactions.legs`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from charli3_dendrite.lending.liqwid.datums import encode_loan_datum
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.legs import loan_output
from charli3_dendrite.lending.liqwid.transactions.terms import check_limits
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.validity import merge_validity
from charli3_dendrite.lending.units import script_payment_address
from charli3_dendrite.utility import posix_ms_to_slot

if TYPE_CHECKING:
    from pycardano import TransactionBuilder
    from pycardano import TransactionOutput

    from charli3_dendrite.lending.liqwid.datums import LoanDatum
    from charli3_dendrite.lending.liqwid.deployment import MarketComponents
    from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
    from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
    from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
    from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
    from charli3_dendrite.lending.liqwid.transactions.terms import Flow
    from charli3_dendrite.lending.transactions.utxos import Utxo

__all__ = [
    "LoanTarget",
    "check_flow_limits",
    "kept_loan_lovelace",
    "loan_target",
    "market_references",
    "merge_validity_ms",
    "required_script",
    "written_loan",
]


def required_script(ref: Utxo | None, market: MarketComponents, what: str) -> Utxo:
    """``ref``, the reference script a flow runs; refused when it is missing.

    ``what`` names the script in the refusal (``"loan validator"``, ...).

    Raises:
        UnsupportedMarketError: ``ref`` is None.
    """
    if ref is None:
        raise UnsupportedMarketError(market.name, f"no {what} reference script")
    return ref


@dataclass(frozen=True)
class LoanTarget:
    """Where a market's loans live: the loan validator's address and token policy.

    ``address`` is the enterprise address of the loan validator the MarketParams
    name (``p[35]``) and ``policy`` the market's current loan-token policy.
    """

    address: str
    policy: str


def loan_target(records: MarketRecords) -> LoanTarget:
    """The loan validator address and loan-token policy of ``records``' market.

    Raises:
        UnsupportedMarketError: The MarketParams name no loan validator, or the
            market has no current loan-token policy.
    """
    market = records.market
    loan_validator = records.params_datum.loan_validator
    if loan_validator is None:
        raise UnsupportedMarketError(market.name, "MarketParams name no loan validator")
    if market.new_borrow_policy is None:
        raise UnsupportedMarketError(market.name, "no current loan-token policy")
    return LoanTarget(
        address=script_payment_address(loan_validator),
        policy=market.new_borrow_policy,
    )


def written_loan(
    target: LoanTarget,
    *,
    datum: LoanDatum,
    indefinite: bool | None,
    loan_token: str,
    collateral: Mapping[str, int],
    previous_lovelace: int | None,
) -> TransactionOutput:
    """The loan output a flow writes at ``target`` (`loan_output`).

    ``datum`` is encoded in the indefinite style when ``indefinite`` is true and
    definite otherwise.

    Raises:
        ValueError: As `loan_output`.
    """
    return loan_output(
        address=target.address,
        datum=encode_loan_datum(datum, indefinite=bool(indefinite)),
        loan_policy=target.policy,
        loan_token=loan_token,
        collateral=collateral,
        previous_lovelace=previous_lovelace,
    )


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
    upper_ms: int | None,
    lower_ms: int | None,
) -> None:
    """Intersect the builder's validity window with bounds in POSIX milliseconds.

    A None bound leaves that side of the window as it is.

    Raises:
        ValidityWindowError: The merged window is empty.
    """
    merge_validity(
        tx_builder,
        upper_slot=None if upper_ms is None else posix_ms_to_slot(upper_ms),
        lower_slot=None if lower_ms is None else posix_ms_to_slot(lower_ms),
    )
