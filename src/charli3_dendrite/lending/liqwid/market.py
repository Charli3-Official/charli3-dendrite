"""Liqwid V2 market states and loans as common lending views.

A MarketState UTxO is one market and a loan UTxO one position. A market view needs
its market's parameters, which live in other UTxOs: the newest decodable MarketParams
record and every CollateralParams record of the market, read from a
:class:`~charli3_dendrite.lending.markets.ParseContext`. A loan converts on its own;
given a context that holds its market, it is bound to that market (index, rate and
per-holding liquidation weights) as of the context.

Collateral terms carry parameter-derived values only (MarketParams, CollateralParams
and the deployment snapshot), so every MarketState between two parameter changes
yields the same terms.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING

from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import _POLICY_HEX_LEN
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.math import INTEREST_PERIOD_MS
from charli3_dendrite.lending.liqwid.math import _fraction
from charli3_dendrite.lending.liqwid.math import apy_fraction
from charli3_dendrite.lending.liqwid.math import supply_apy
from charli3_dendrite.lending.liqwid.math import utilization
from charli3_dendrite.lending.liqwid.rates import PROTOCOL_NAME
from charli3_dendrite.lending.liqwid.rates import LiqwidVariableRate
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import CollateralTerms
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.normalized import Origin
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource

if TYPE_CHECKING:
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
    from charli3_dendrite.lending.liqwid.deployment import MarketComponents
    from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
    from charli3_dendrite.lending.markets import ParseContext

_MS_PER_SECOND = 1000
# Errors a malformed record raises while it is converted.
_CONVERSION_ERRORS = (ArithmeticError, KeyError, TypeError, ValueError)


def _resolved(deployment: LiqwidDeployment | None) -> LiqwidDeployment:
    return default_deployment() if deployment is None else deployment


def _chain_order(info: PoolStateInfo) -> tuple[int, int, str, int]:
    """Where a UTxO sits in the chain: block time, transaction in block, out-ref."""
    return (info.block_time, info.block_index, info.tx_hash, info.tx_index)


def _unit_order(unit: str) -> str:
    """Canonical unit order: lovelace first, then by policy id and asset name."""
    return "" if unit == "lovelace" else unit


def _market_records(
    context: ParseContext,
    market_id: str,
    deployment: LiqwidDeployment,
) -> dict[LiqwidKind, list[LiqwidRecord]]:
    """The records of ``market_id`` in ``context`` by kind, each newest first.

    A UTxO that is not a decodable record (a MarketParams parked on an empty
    constructor, dust, another protocol's output) is skipped.
    """
    by_kind: dict[LiqwidKind, list[LiqwidRecord]] = {}
    for info in context.utxos:
        record = parse_utxo(info, deployment)
        if record is not None and record.market_id == market_id:
            by_kind.setdefault(record.kind, []).append(record)
    for records in by_kind.values():
        records.sort(key=lambda record: _chain_order(record.info), reverse=True)
    return by_kind


def _parameters(
    market: MarketComponents,
    by_kind: dict[LiqwidKind, list[LiqwidRecord]],
    slot: int | None,
) -> tuple[MarketParamsDatum, tuple[CollateralParamsDatum, ...]]:
    """:func:`market_parameters` over records already grouped by kind."""
    params = by_kind.get(LiqwidKind.MARKET_PARAMS, [])
    newest = params[0].datum if params else None
    if not isinstance(newest, MarketParamsDatum):
        raise LendingParseError(
            f"no market parameters at slot {slot} for market {market.name}",
        )
    collateral: dict[tuple[str, bool], CollateralParamsDatum] = {}
    for record in by_kind.get(LiqwidKind.COLLATERAL_PARAMS, []):
        datum = record.datum
        if isinstance(datum, CollateralParamsDatum):
            collateral.setdefault((datum.collateral, datum.policy_wide), datum)
    keys = sorted(collateral, key=lambda key: (_unit_order(key[0]), key[1]))
    return newest, tuple(collateral[key] for key in keys)


def market_parameters(
    market: MarketComponents,
    context: ParseContext,
    deployment: LiqwidDeployment | None = None,
) -> tuple[MarketParamsDatum, tuple[CollateralParamsDatum, ...]]:
    """The parameters of ``market`` in force in ``context``.

    Returns the newest decodable MarketParams record of the market and its newest
    CollateralParams record per collateral, in canonical unit order. Records of other
    markets are ignored; a market without CollateralParams records has none.

    Raises:
        LendingParseError: if ``context`` holds no decodable MarketParams record of
            the market.
    """
    deployment = _resolved(deployment)
    by_kind = _market_records(context, market.market_id, deployment)
    return _parameters(market, by_kind, context.slot)


def collateral_terms(
    params: MarketParamsDatum,
    record: CollateralParamsDatum,
    deployment: LiqwidDeployment | None = None,
) -> CollateralTerms:
    """One CollateralParams record of a market as collateral terms.

    With weight ``w``: ``max_ltv`` is the max LTV p[12] times ``w`` and
    ``liquidation_ltv`` is ``w`` times the liquidation threshold p[18] (1 on every
    live market, where health is the weighted collateral value over the debt);
    ``weight`` keeps ``w`` itself. A whole-policy record (``C0[policy]``) gives
    policy-wide terms. The price source names the collateral's underlying and the
    MarketState NFT policy of its market, from the deployment snapshot when it knows
    the qToken and from the record's own link otherwise.
    """
    deployment = _resolved(deployment)
    weight = _fraction(record.weight)
    linked = (
        None if record.policy_wide else deployment.market_of_qtoken(record.collateral)
    )
    underlying = record.underlying if linked is None else linked.underlying
    market_state = (
        record.collateral_market_state if linked is None else linked.market_state_policy
    )
    return CollateralTerms(
        unit=record.collateral,
        policy_wide=record.policy_wide,
        max_ltv=_fraction(params.max_ltv) * weight,
        fixed_ratio=None,
        liquidation_ltv=weight * _fraction(params.liquidation_threshold),
        liquidation_penalty=None,
        liquidation_discount=1 - _fraction(record.liquidation_discount),
        equity_to_borrower=True,
        price_source=OracleRef(
            source=OracleSource.LIQWID,
            token=record.collateral,
            extra={"underlying": underlying, "market_state": market_state},
        ),
        weight=weight,
    )


def market_to_view(
    state: LiqwidRecord,
    context: ParseContext,
    deployment: LiqwidDeployment | None = None,
) -> LendingMarket:
    """A MarketState record as a market, under the parameters in ``context``.

    Raises:
        LendingParseError: if ``state`` is not a MarketState record, ``context``
            holds no decodable MarketParams record of its market, or a value cannot
            be converted.
    """
    return _market_view(state, context, _resolved(deployment))


def _market_view(
    state: LiqwidRecord,
    context: ParseContext,
    deployment: LiqwidDeployment,
    by_kind: dict[LiqwidKind, list[LiqwidRecord]] | None = None,
) -> LendingMarket:
    """:func:`market_to_view` over the market's records grouped by kind.

    ``by_kind`` is grouped from ``context`` when the caller has not done so already.
    """
    datum = state.datum
    if (
        state.kind is not LiqwidKind.MARKET_STATE
        or not isinstance(datum, MarketStateDatum)
        or state.market_id is None
    ):
        raise LendingParseError(f"{state.out_ref} is not a Liqwid V2 MarketState")
    try:
        market = deployment.market(state.market_id)
    except KeyError as exc:
        raise LendingParseError(
            f"market state {state.out_ref}: unknown market {state.market_id}",
        ) from exc
    if by_kind is None:
        by_kind = _market_records(context, market.market_id, deployment)
    params, records = _parameters(market, by_kind, context.slot)
    try:
        util = utilization(datum)
        rate_num, rate_den = datum.rate
        min_rate_num, min_rate_den = params.min_interest_rate
        return LendingMarket(
            protocol=PROTOCOL_NAME,
            market_id=market.market_id,
            borrow_unit=market.underlying,
            lender=None,
            available_liquidity=datum.cash,
            rate_model=LiqwidVariableRate(
                rate_num=rate_num,
                rate_den=rate_den,
                period_ms=params.interest_period_ms,
                market_index=datum.index,
                min_rate_num=min_rate_num,
                min_rate_den=min_rate_den,
            ),
            supply_rate=apy_fraction(
                supply_apy(
                    _fraction(datum.rate),
                    util,
                    params.income_ratio,
                    params.interest_period_ms,
                ),
            ),
            receipt_unit=market.qtoken_policy,
            utilization=util,
            collateral=tuple(
                collateral_terms(params, record, deployment) for record in records
            ),
            on_default=DefaultRule.PRICE_LIQUIDATION,
            permissioned=False,
            receipt_rate=_fraction(datum.qtoken_rate),
            max_utilization=(
                None if params.borrow_cap is None else _fraction(params.borrow_cap)
            ),
            total_principal=datum.principal,
        )
    except _CONVERSION_ERRORS as exc:
        raise LendingParseError(f"market state {state.out_ref}: {exc}") from exc


def _holdings(
    info: PoolStateInfo,
    deployment: LiqwidDeployment,
) -> tuple[CollateralHolding, ...]:
    """Every unit a loan UTxO holds except ADA and loan tokens, in canonical order.

    A qToken holding is its bare 56-hex policy with the literal amount; a holding of
    a whole-policy collateral keeps its full policy and asset name.
    """
    loan_tokens = deployment.loan_token_policies
    units = sorted(
        (
            unit
            for unit in info.assets
            if unit != "lovelace" and unit[:_POLICY_HEX_LEN] not in loan_tokens
        ),
        key=_unit_order,
    )
    return tuple(CollateralHolding(unit, int(info.assets[unit])) for unit in units)


def _context_market(
    market: MarketComponents,
    context: ParseContext,
    deployment: LiqwidDeployment,
) -> LendingMarket | None:
    """``market`` as of ``context``; None without its MarketState or parameters."""
    by_kind = _market_records(context, market.market_id, deployment)
    states = by_kind.get(LiqwidKind.MARKET_STATE)
    if not states:
        return None
    try:
        return _market_view(states[0], context, deployment, by_kind)
    except LendingParseError:
        return None


def loan_to_position(
    loan: LiqwidRecord,
    context: ParseContext | None = None,
    deployment: LiqwidDeployment | None = None,
) -> LendingPosition:
    """A loan record as a position, identified by its out-ref.

    Without a context (or with one that lacks the loan's MarketState or market
    parameters) the position is unbound: its rate model carries ``market_index =
    loan_index`` and a zero rate, and no holding has a ``liquidation_ltv``;
    :meth:`LendingPosition.at_market` binds it later. With such a context it is
    bound to that market: the market index s5 and rate of its newest MarketState,
    and each holding's ``liquidation_ltv`` (``w * p[18]``) from the market's terms.
    ``opened_ms`` is this UTxO's block time; ``PoolStateInfo.block_time`` must be
    POSIX seconds.

    Raises:
        LendingParseError: if ``loan`` is not a loan record of a known market.
    """
    deployment = _resolved(deployment)
    datum = loan.datum
    if (
        loan.kind is not LiqwidKind.LOAN
        or not isinstance(datum, LoanDatum)
        or loan.market_id is None
    ):
        raise LendingParseError(f"{loan.out_ref} is not a Liqwid V2 loan")
    try:
        market = deployment.market(loan.market_id)
        position = LendingPosition(
            protocol=PROTOCOL_NAME,
            position_id=loan.out_ref,
            origin=Origin("market", market.market_id),
            borrower=Party(PartyKind.KEY, datum.owner),
            lender=None,
            borrow_unit=market.underlying,
            principal=datum.principal,
            installments_paid=0,
            collateral=_holdings(loan.info, deployment),
            rate_model=LiqwidVariableRate(
                rate_num=0,
                rate_den=1,
                period_ms=INTEREST_PERIOD_MS,
                market_index=datum.index,
                loan_index=datum.index,
                interest=datum.interest,
                min_interest=datum.min_interest,
            ),
            liquidation_ltv=Fraction(1),
            on_default=DefaultRule.PRICE_LIQUIDATION,
            opened_ms=loan.info.block_time * _MS_PER_SECOND,
        )
    except _CONVERSION_ERRORS as exc:
        raise LendingParseError(f"loan {loan.out_ref}: {exc}") from exc
    if context is None:
        return position
    bound = _context_market(market, context, deployment)
    return position if bound is None else position.at_market(bound)
