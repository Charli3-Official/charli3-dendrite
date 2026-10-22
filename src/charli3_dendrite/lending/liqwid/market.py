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

import charli3_dendrite.lending.liqwid.oracles  # noqa: F401
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import newest_first
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.lifecycle import LiqwidLifecycle
from charli3_dendrite.lending.liqwid.lifecycle import collateral_units
from charli3_dendrite.lending.liqwid.loader import snapshot
from charli3_dendrite.lending.liqwid.math import INTEREST_PERIOD_MS
from charli3_dendrite.lending.liqwid.math import _fraction
from charli3_dendrite.lending.liqwid.math import apy_fraction
from charli3_dendrite.lending.liqwid.math import liquidation_weight
from charli3_dendrite.lending.liqwid.math import supply_apy
from charli3_dendrite.lending.liqwid.math import utilization
from charli3_dendrite.lending.liqwid.rates import PROTOCOL_NAME
from charli3_dendrite.lending.liqwid.rates import RATE_MODELS  # noqa: F401
from charli3_dendrite.lending.liqwid.rates import LiqwidVariableRate
from charli3_dendrite.lending.markets import LendingSnapshot
from charli3_dendrite.lending.markets import ParseContext
from charli3_dendrite.lending.markets import convertible_views
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import CollateralTerms
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.normalized import Origin
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.normalized import as_parse_error
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.units import canonical_unit_key

if TYPE_CHECKING:
    from collections.abc import Mapping

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
    from charli3_dendrite.lending.liqwid.deployment import MarketComponents
    from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
    from charli3_dendrite.lending.markets import WatchSpec

_MS_PER_SECOND = 1000
# Errors a malformed record raises while it is converted.
_CONVERSION_ERRORS = (ArithmeticError, KeyError, TypeError, ValueError)


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
    return {kind: newest_first(records) for kind, records in by_kind.items()}


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
    keys = sorted(collateral, key=lambda key: (canonical_unit_key(key[0]), key[1]))
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
    deployment = resolve_deployment(deployment)
    by_kind = _market_records(context, market.market_id, deployment)
    return _parameters(market, by_kind, context.slot)


def collateral_terms(
    params: MarketParamsDatum,
    record: CollateralParamsDatum,
    deployment: LiqwidDeployment | None = None,
) -> CollateralTerms:
    """One CollateralParams record of a market as collateral terms.

    With weight ``w``: ``max_ltv`` is the max LTV p[12] times ``w`` and
    ``liquidation_ltv`` is ``liquidation_weight``, ``w`` times the liquidation
    threshold p[18] (1 on every live market, where health is the weighted collateral
    value over the debt); ``weight`` keeps ``w`` itself. A whole-policy record
    (``C0[policy]``) gives policy-wide terms. The price source names the
    collateral's underlying and the MarketState NFT policy of its market, from the
    deployment snapshot when it knows the qToken and from the record's own link
    otherwise.
    """
    deployment = resolve_deployment(deployment)
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
        liquidation_ltv=liquidation_weight(record.weight, params.liquidation_threshold),
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
    return _market_view(state, context, resolve_deployment(deployment))


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
        raise as_parse_error(f"market state {state.out_ref}", exc) from exc


def _holdings(
    info: PoolStateInfo,
    deployment: LiqwidDeployment,
) -> tuple[CollateralHolding, ...]:
    """A loan UTxO's :func:`collateral_units` as holdings, in canonical order."""
    return tuple(
        CollateralHolding(unit, int(quantity))
        for unit, quantity in collateral_units(info, deployment.loan_token_policies)
    )


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
    *,
    markets: Mapping[str, LendingMarket] | None = None,
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

    ``markets``, when given with a context, maps market ids to their views as of
    that context (each market's newest MarketState; a market whose state does not
    convert is absent), so a caller converting many loans against one context
    builds each market once.

    Raises:
        LendingParseError: if ``loan`` is not a loan record of a known market.
    """
    deployment = resolve_deployment(deployment)
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
        raise as_parse_error(f"loan {loan.out_ref}", exc) from exc
    if context is None:
        return position
    bound = (
        _context_market(market, context, deployment)
        if markets is None
        else markets.get(market.market_id)
    )
    return position if bound is None else position.at_market(bound)


def _newest_views(
    states: tuple[LiqwidRecord, ...],
    views: Mapping[str, LendingMarket],
) -> dict[str, LendingMarket]:
    """Each market's view of its newest MarketState in ``states``, by market id.

    ``views`` holds the converted states by out-ref; a market whose newest state did
    not convert is absent, as :func:`loan_to_position` leaves such a loan unbound.
    """
    newest: dict[str, LiqwidRecord] = {}
    for state in newest_first(states):
        if state.market_id is not None:
            newest.setdefault(state.market_id, state)
    return {
        market_id: views[state.out_ref]
        for market_id, state in newest.items()
        if state.out_ref in views
    }


class LiqwidV2MarketSource:
    """Liqwid V2 markets and loans as common views.

    A MarketState UTxO is a market and a loan UTxO a position. MarketParams,
    CollateralParams and oracle feeds are context: they shape the views and are never
    views themselves. A loan has no identity token, so :attr:`lifecycle` follows each
    loan across the UTxOs that re-create it.
    """

    name = PROTOCOL_NAME
    lifecycle: LiqwidLifecycle

    def __init__(self, deployment: LiqwidDeployment | None = None) -> None:
        """Watch every generation ``deployment`` names; mainnet by default."""
        self._deployment = resolve_deployment(deployment)
        self._selectors = liqwid_selectors(self._deployment)
        self.lifecycle = LiqwidLifecycle(self._deployment)

    def selectors(self) -> list[WatchSpec]:
        """MarketState and loan credentials (views), then the context ones."""
        return [selector.watch_spec() for selector in self._selectors]

    def parse(
        self,
        info: PoolStateInfo,
        *,
        context: ParseContext | None = None,
    ) -> LendingMarket | LendingPosition | None:
        """One UTxO as its view; None when it is not a Liqwid V2 MarketState or loan.

        A MarketState needs its market's parameters from ``context`` and raises
        LendingParseError without them (also when ``context`` is None). A loan parses
        with or without ``context``; with it, the loan carries the market index and
        collateral weights as of that point. MarketParams, CollateralParams and oracle
        UTxOs return None.
        """
        record = parse_utxo(info, self._deployment)
        if record is None:
            return None
        if record.kind is LiqwidKind.MARKET_STATE:
            given = ParseContext(slot=None) if context is None else context
            return market_to_view(record, given, self._deployment)
        if record.kind is LiqwidKind.LOAN:
            return loan_to_position(record, context, self._deployment)
        return None

    def load(
        self,
        backend: AbstractBackend,
        now_ms: int | None = None,
    ) -> LendingSnapshot:
        """Every live Liqwid V2 market and loan, read against the live context.

        Each market's view is built once and shared by its loans. UTxOs that raise
        LendingParseError are left out.
        """
        deployment = self._deployment
        found = snapshot(backend, now_ms, deployment)

        def market(state: LiqwidRecord) -> tuple[str, LendingMarket]:
            return state.out_ref, market_to_view(state, found.context, deployment)

        views = dict(convertible_views(market, found.market_states))
        bound = _newest_views(found.market_states, views)

        def position(loan: LiqwidRecord) -> LendingPosition:
            return loan_to_position(loan, found.context, deployment, markets=bound)

        return LendingSnapshot(
            markets=tuple(views.values()),
            positions=convertible_views(position, found.loans),
        )


def market_source() -> LiqwidV2MarketSource:
    """The mainnet Liqwid V2 source, for :mod:`charli3_dendrite.lending.builtins`.

    Importing this module registers ``LiqwidOracleResolver`` (through
    :mod:`charli3_dendrite.lending.liqwid.oracles`), so the built-ins load also makes
    Liqwid's on-chain feeds resolvable.
    """
    return LiqwidV2MarketSource()
