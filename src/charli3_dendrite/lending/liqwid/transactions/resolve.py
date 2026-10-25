"""Live resolution of what a Liqwid V2 transaction spends and references.

Each resolver reads current chain state through a backend and returns resolved
:class:`~charli3_dendrite.lending.transactions.utxos.Utxo` s or decoded records:

- :func:`resolve_script_refs`: the reference-script UTxOs of a market's action
  validator, qToken policy, loan validator, loan-token policy and the two staking
  validators, from the out-refs the deployment records for their hashes;
- :func:`resolve_action_utxos` and :func:`choose_action_utxo`: the market's live
  action UTxOs and the choice of the one a transaction spends;
- :func:`resolve_market_records`: each market's newest MarketParams, MarketState
  and CollateralParams records, from one fetch;
- :func:`resolve_loan`: a loan by its out-ref;
- :func:`resolve_feeds` and :func:`resolve_health_inputs`: the oracle feeds,
  CollateralParams and MarketStates a health check reads;
- :func:`resolve_now_ms`: the chain tip's time.

Reference scripts and loans are resolved by out-ref through the dbsync-only
``db_query`` (:mod:`charli3_dendrite.lending.transactions.resolve`); records, action
UTxOs and feeds through ``get_pool_utxos``, which must select by payment credential
as the dbsync backend does.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from charli3_dendrite.lending.fetch import PAGE_SIZE
from charli3_dendrite.lending.liqwid.datums import POLICY_KEY_PREFIX
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import LEGACY_GENERATION
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.deployment import staking_roles
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
from charli3_dendrite.lending.liqwid.indexing import LiqwidSelector
from charli3_dendrite.lending.liqwid.indexing import action_selector
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import newest_first
from charli3_dendrite.lending.liqwid.indexing import param_selector
from charli3_dendrite.lending.liqwid.indexing import parse_action
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.indexing import state_selectors
from charli3_dendrite.lending.liqwid.loader import fetch_records
from charli3_dendrite.lending.liqwid.oracles import LiqwidFeed
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.liqwid.oracles import select_feeds
from charli3_dendrite.lending.liqwid.transactions.errors import ActionUtxoSpentError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import LiqwidBuildError
from charli3_dendrite.lending.liqwid.transactions.errors import MissingFeedError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    UnsupportedCollateralError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedLoanError
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.transactions.infra import current_slot
from charli3_dendrite.lending.transactions.resolve import resolve_utxo_by_outref
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.lending.units import out_ref_key
from charli3_dendrite.lending.units import policy_of
from charli3_dendrite.lending.units import script_credential
from charli3_dendrite.lending.units import sole_token_name
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    import random

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo

__all__ = [
    "ActionUtxo",
    "HealthInputs",
    "LiqwidScriptRefs",
    "LoanRecord",
    "MarketRecords",
    "choose_action_utxo",
    "collateral_params_records",
    "record_from_utxo",
    "resolve_action_utxos",
    "resolve_feeds",
    "resolve_health_inputs",
    "resolve_loan",
    "resolve_market_records",
    "resolve_now_ms",
    "resolve_script_refs",
]

# The MarketParams layout that carries the loan validator (p[35]) and the staking
# pair (p[34]); loan flows need both.
_LOAN_LAYOUT = 36


# --- reference scripts -------------------------------------------------------


@dataclass(frozen=True)
class LiqwidScriptRefs:
    """The reference-script UTxOs of one market's scripts.

    ``action`` carries the action validator its MarketParams names (``p[20]``) and
    ``qtoken_policy`` the qToken minting policy. The loan scripts (``p[35]``, the
    current loan-token policy and the two ``p[34]`` staking validators by role) are
    None when they were not resolved.
    """

    action: Utxo
    qtoken_policy: Utxo
    loan_validator: Utxo | None
    loan_policy: Utxo | None
    borrow_staking: Utxo | None
    liquidation_staking: Utxo | None


def _out_ref_text(utxo: Utxo) -> str:
    if utxo.out_ref is None:
        msg = "a resolved UTxO carries no out-ref"
        raise ValueError(msg)
    return f"{utxo.out_ref[0]}#{utxo.out_ref[1]}"


def _reference_script(
    backend: AbstractBackend,
    market: MarketComponents,
    deployment: LiqwidDeployment,
    script_hash: str,
) -> Utxo:
    """The unspent UTxO carrying ``script_hash``, checked against the deployment.

    Raises:
        UnsupportedMarketError: the deployment records no reference UTxO for the
            script, the UTxO is spent or missing, or it does not carry the script in
            the recorded language.
    """
    try:
        ref = deployment.reference_utxo(script_hash)
    except KeyError:
        raise UnsupportedMarketError(
            market.name,
            f"no reference UTxO for script {script_hash}",
        ) from None
    tx_hash, _, index = ref.out_ref.partition("#")
    try:
        utxo = resolve_utxo_by_outref(backend, tx_hash, int(index))
    except ValueError:
        raise UnsupportedMarketError(
            market.name,
            f"reference UTxO {ref.out_ref} for script {script_hash} is not unspent",
        ) from None
    if not utxo.ref_script or utxo.ref_script_type != ref.language:
        raise UnsupportedMarketError(
            market.name,
            f"reference UTxO {ref.out_ref} carries no {ref.language} script",
        )
    found = script_hash_of(utxo)
    if found != script_hash:
        raise UnsupportedMarketError(
            market.name,
            f"reference UTxO {ref.out_ref} hashes to {found}, not {script_hash}",
        )
    return utxo


def resolve_script_refs(
    backend: AbstractBackend,
    market: MarketComponents,
    params: MarketParamsDatum,
    *,
    deployment: LiqwidDeployment | None = None,
    loans: bool = True,
) -> LiqwidScriptRefs:
    """Resolve the reference-script UTxOs a transaction on ``market`` needs.

    Each script is found by the out-ref ``deployment`` (the bundled one by default)
    records for its hash and must be unspent, carry its script in the recorded
    language (PlutusV2) and hash to it. The action validator is the one ``params``
    names (``p[20]``), which need not be the newest the deployment lists; the
    staking validators are ``p[34]``'s, placed by role. ``loans=False`` resolves only
    the action validator and the qToken policy (supply and redeem).

    Raises:
        UnsupportedMarketError: ``params`` names no action validator; with
            ``loans``, its layout is shorter than 36 fields, its staking pair is not
            one script of each role, or the market has no current loan-token
            policy; or a reference UTxO fails the checks above.
    """
    active = resolve_deployment(deployment)
    if params.action_validator is None:
        raise UnsupportedMarketError(market.name, "MarketParams names no action")
    action = _reference_script(backend, market, active, params.action_validator)
    qtoken = _reference_script(backend, market, active, market.qtoken_policy)
    if not loans:
        return LiqwidScriptRefs(action, qtoken, None, None, None, None)
    if params.length < _LOAN_LAYOUT or params.loan_validator is None:
        raise UnsupportedMarketError(
            market.name,
            f"loan flows need the 36-field MarketParams (got {params.length})",
        )
    if market.new_borrow_policy is None:
        raise UnsupportedMarketError(market.name, "no current loan-token policy")
    try:
        roles = staking_roles(market, params)
    except ValueError as exc:
        raise UnsupportedMarketError(market.name, str(exc)) from None
    return LiqwidScriptRefs(
        action=action,
        qtoken_policy=qtoken,
        loan_validator=_reference_script(
            backend,
            market,
            active,
            params.loan_validator,
        ),
        loan_policy=_reference_script(
            backend,
            market,
            active,
            market.new_borrow_policy,
        ),
        borrow_staking=_reference_script(backend, market, active, roles.borrow),
        liquidation_staking=_reference_script(
            backend,
            market,
            active,
            roles.liquidation,
        ),
    )


# --- action UTxOs ------------------------------------------------------------


@dataclass(frozen=True)
class ActionUtxo:
    """One of a market's action UTxOs with its decoded pending deltas."""

    utxo: Utxo
    datum: ActionDatum

    @property
    def out_ref(self) -> str:
        """The UTxO's ``<tx hash>#<index>``."""
        return _out_ref_text(self.utxo)

    def underlying_amount(self, underlying: str) -> int:
        """The quantity of ``underlying`` (``"lovelace"`` or a unit) the UTxO holds."""
        if underlying == "lovelace":
            return self.utxo.lovelace
        return sum(
            qty for policy, name, qty in self.utxo.assets if policy + name == underlying
        )


def resolve_action_utxos(
    backend: AbstractBackend,
    market: MarketComponents,
    params: MarketParamsDatum,
    *,
    page_size: int = PAGE_SIZE,
) -> list[ActionUtxo]:
    """Every live action UTxO of ``market``, in out-ref order.

    Reads the holders of the market's action token at the action validator
    ``params`` names (``p[20]``), page by page, until a short page or one that adds
    no out-ref. A UTxO counts when it sits at that credential and
    :func:`~charli3_dendrite.lending.liqwid.indexing.parse_action` accepts it.

    Raises:
        UnsupportedMarketError: ``params`` names no action validator or the market
            has no action-token policy.
    """
    try:
        selector = action_selector(market, params)
    except ValueError as exc:
        raise UnsupportedMarketError(market.name, str(exc)) from None
    found: dict[str, ActionUtxo] = {}
    page = 0
    while True:
        rows = list(
            backend.get_pool_utxos(
                addresses=[selector.address],
                assets=[selector.identity_policy],
                limit=page_size,
                page=page,
                historical=False,
            ),
        )
        before = len(found)
        for info in rows:
            record = _action_record(info, market, selector)
            if record is not None:
                found.setdefault(record.out_ref, record)
        if len(rows) < page_size or len(found) == before:
            break
        page += 1
    return [found[ref] for ref in sorted(found, key=out_ref_key)]


def _action_record(
    info: PoolStateInfo,
    market: MarketComponents,
    selector: LiqwidSelector,
) -> ActionUtxo | None:
    if script_credential(info.address) != selector.payment_credential:
        return None
    record = parse_action(info, market)
    if record is None or not isinstance(record.datum, ActionDatum):
        return None
    return ActionUtxo(utxo=utxo_from_info(info), datum=record.datum)


def choose_action_utxo(
    utxos: Sequence[ActionUtxo],
    *,
    check: Callable[[ActionUtxo], None],
    pin: str | None = None,
    exclude: Collection[str] = (),
    rng: random.Random | None = None,
) -> ActionUtxo:
    """The action UTxO a transaction spends.

    A ``pin`` (``<tx hash>#<index>``) wins: it must be among ``utxos`` and not in
    ``exclude`` (the out-refs already reported spent), and it must pass ``check``.
    Otherwise the choice is uniform, by ``rng`` (a system random source by
    default), among the UTxOs not excluded that pass ``check``; ``check`` raises a
    build error for a UTxO that cannot serve the transaction.

    Raises:
        ActionUtxoSpentError: the pin is not among ``utxos`` or is excluded.
        LiqwidBuildError: the pin fails ``check``, or no UTxO passes it (the last
            error ``check`` raised); ``InsufficientLiquidityError(0, 0)`` when no
            UTxO is left to check.
    """
    if pin is not None:
        pinned = next((u for u in utxos if u.out_ref == pin), None)
        if pinned is None or pin in exclude:
            raise ActionUtxoSpentError(pin)
        check(pinned)
        return pinned
    eligible: list[ActionUtxo] = []
    last_error: LiqwidBuildError | None = None
    for utxo in utxos:
        if utxo.out_ref in exclude:
            continue
        try:
            check(utxo)
        except LiqwidBuildError as exc:
            last_error = exc
            continue
        eligible.append(utxo)
    if eligible:
        return (rng or secrets.SystemRandom()).choice(eligible)
    if last_error is not None:
        raise last_error
    raise InsufficientLiquidityError(0, 0)


# --- market records ----------------------------------------------------------


@dataclass(frozen=True)
class MarketRecords:
    """One market's newest live MarketParams, MarketState and CollateralParams.

    ``collateral_params`` maps each exact collateral unit to its newest record
    (:func:`collateral_params_records`).
    """

    market: MarketComponents
    params: LiqwidRecord
    state: LiqwidRecord
    collateral_params: Mapping[str, LiqwidRecord]

    @property
    def params_datum(self) -> MarketParamsDatum:
        """The decoded MarketParams record."""
        datum = self.params.datum
        if not isinstance(datum, MarketParamsDatum):
            msg = f"liqwid record {self.params.out_ref} is not a MarketParams record"
            raise TypeError(msg)
        return datum

    @property
    def state_datum(self) -> MarketStateDatum:
        """The decoded MarketState record."""
        datum = self.state.datum
        if not isinstance(datum, MarketStateDatum):
            msg = f"liqwid record {self.state.out_ref} is not a MarketState record"
            raise TypeError(msg)
        return datum


def collateral_params_records(
    records: Iterable[LiqwidRecord],
    market: MarketComponents,
) -> dict[str, LiqwidRecord]:
    """``market``'s newest CollateralParams record per exact collateral unit.

    Records of other kinds and markets are ignored, and so are policy-wide records
    (``C0[policy]``): a collateral is priced only through an exact record. Newest is
    the latest chain position.
    """
    chosen: dict[str, LiqwidRecord] = {}
    for record in newest_first(records):
        datum = record.datum
        if (
            record.kind is LiqwidKind.COLLATERAL_PARAMS
            and record.market_id == market.market_id
            and isinstance(datum, CollateralParamsDatum)
            and not datum.policy_wide
        ):
            chosen.setdefault(datum.collateral, record)
    return chosen


def _market_selectors(
    deployment: LiqwidDeployment,
    market: MarketComponents,
) -> list[LiqwidSelector]:
    return [
        param_selector(deployment, LiqwidKind.MARKET_PARAMS, market),
        param_selector(deployment, LiqwidKind.COLLATERAL_PARAMS, market),
        *state_selectors(deployment, market),
    ]


def _newest(
    records: Iterable[LiqwidRecord],
    kind: LiqwidKind,
    market: MarketComponents,
) -> LiqwidRecord:
    for record in newest_first(records):
        if record.kind is kind and record.market_id == market.market_id:
            return record
    raise UnsupportedMarketError(market.name, f"no live {kind.value} record")


def resolve_market_records(
    backend: AbstractBackend,
    markets: Iterable[MarketComponents],
    *,
    deployment: LiqwidDeployment | None = None,
) -> dict[str, MarketRecords]:
    """The live records of each of ``markets``, keyed by market name.

    One fetch reads every market's MarketParams, CollateralParams and MarketState
    selectors; each address is read once.

    Raises:
        UnsupportedMarketError: a market has no live MarketParams or MarketState.
    """
    active = resolve_deployment(deployment)
    wanted = list(markets)
    selectors = [s for market in wanted for s in _market_selectors(active, market)]
    records = fetch_records(backend, selectors, deployment=deployment)
    return {
        market.name: MarketRecords(
            market=market,
            params=_newest(records, LiqwidKind.MARKET_PARAMS, market),
            state=_newest(records, LiqwidKind.MARKET_STATE, market),
            collateral_params=collateral_params_records(records, market),
        )
        for market in wanted
    }


def record_from_utxo(
    u: Utxo,
    *,
    deployment: LiqwidDeployment | None = None,
) -> LiqwidRecord | None:
    """The routed Liqwid record ``u`` holds, or None (as :func:`parse_utxo`)."""
    return parse_utxo(utxo_to_info(u), deployment)


# --- loans -------------------------------------------------------------------


@dataclass(frozen=True)
class LoanRecord:
    """A current-generation loan: its UTxO, record, market and loan-token name."""

    utxo: Utxo
    datum: LoanDatum
    market: MarketComponents
    token_name: str


def resolve_loan(
    backend: AbstractBackend,
    out_ref: str,
    *,
    deployment: LiqwidDeployment | None = None,
    allow_spent: bool = False,
) -> LoanRecord:
    """Resolve the loan at ``out_ref`` (``<tx hash>#<index>``).

    ``allow_spent`` resolves an already consumed loan (a captured replay).

    Raises:
        UnsupportedLoanError: the output is a legacy-generation loan (at a legacy
            loan validator or holding a legacy loan token) or is no loan at all.
        ValueError: no such output (or it is spent and ``allow_spent`` is unset).
    """
    active = resolve_deployment(deployment)
    tx_hash, _, index = out_ref.partition("#")
    utxo = resolve_utxo_by_outref(backend, tx_hash, int(index), allow_spent=allow_spent)
    record = parse_utxo(utxo_to_info(utxo), deployment)
    if record is None or record.kind is not LiqwidKind.LOAN or not record.market_id:
        legacy = {m.borrow_policy for m in active.markets} - {None}
        if any(policy in legacy for policy, _, _ in utxo.assets):
            raise UnsupportedLoanError(out_ref, "legacy-generation loan token")
        raise UnsupportedLoanError(out_ref, "not a Liqwid V2 loan")
    if record.generation == LEGACY_GENERATION:
        raise UnsupportedLoanError(out_ref, "legacy-generation loan validator")
    market = active.market(record.market_id)
    token_name = (
        sole_token_name(record.info.assets, market.new_borrow_policy)
        if market.new_borrow_policy is not None
        else None
    )
    if token_name is None or not isinstance(record.datum, LoanDatum):
        raise UnsupportedLoanError(out_ref, "no current loan token")
    return LoanRecord(
        utxo=utxo,
        datum=record.datum,
        market=market,
        token_name=token_name,
    )


# --- feeds and health inputs -------------------------------------------------


def resolve_feeds(
    backend: AbstractBackend,
    assets: Iterable[str],
    *,
    deployment: LiqwidDeployment | None = None,
) -> dict[str, LiqwidFeed]:
    """The oracle feed pricing each of ``assets``, keyed by the asset.

    Every live feed is read and the freshest per priced asset kept
    (:func:`~charli3_dendrite.lending.liqwid.oracles.select_feeds`). An asset takes
    its exact feed when there is one, else the policy-wide feed of its policy.

    Raises:
        MissingFeedError: no feed prices an asset.
    """
    selectors = [
        selector
        for selector in liqwid_selectors(deployment)
        if selector.kind is LiqwidKind.ORACLE
    ]
    feeds = [
        feed
        for record in fetch_records(backend, selectors, deployment=deployment)
        if (feed := parse_feed(record.info, deployment)) is not None
    ]
    selected = select_feeds(feeds)
    found: dict[str, LiqwidFeed] = {}
    for asset in assets:
        policy = policy_of(asset)
        feed = selected.get(asset) or (
            selected.get(POLICY_KEY_PREFIX + policy) if policy else None
        )
        if feed is None:
            raise MissingFeedError(asset)
        found[asset] = feed
    return found


@dataclass(frozen=True)
class HealthInputs:
    """The records a loan's health check reads.

    ``cparams`` maps each collateral unit to its CollateralParams record,
    ``states`` each involved market id (the borrow market and every collateral's
    market) to its MarketState record, and ``feeds`` each underlying unit (the
    borrowed one and every collateral's) to its oracle feed.
    """

    cparams: Mapping[str, LiqwidRecord]
    states: Mapping[str, LiqwidRecord]
    feeds: Mapping[str, LiqwidFeed]


def _collateral_market(
    unit: str,
    datum: CollateralParamsDatum,
    deployment: LiqwidDeployment,
) -> MarketComponents | None:
    """The market whose qToken ``unit`` is, else the record's own market link."""
    linked = deployment.market_of_qtoken(policy_of(unit))
    if linked is None and datum.collateral_market_state:
        linked = deployment.market_of_state_policy(
            policy_of(datum.collateral_market_state),
        )
    return linked


def resolve_health_inputs(
    backend: AbstractBackend,
    units: Iterable[str],
    *,
    borrow: MarketRecords,
    deployment: LiqwidDeployment | None = None,
) -> HealthInputs:
    """Resolve the CollateralParams, MarketStates and feeds a health check reads.

    ``units`` are the collateral units the check values (an owner flow passes the
    output collateral, a liquidation the input collateral); ``borrow`` is the borrow
    market's records, whose CollateralParams price them. A collateral of the borrow
    market itself (a wash borrow) shares its MarketState and feed.

    Raises:
        UnsupportedCollateralError: a unit has no exact CollateralParams record of
            the borrow market, or the record names no collateral market or
            underlying.
        UnsupportedMarketError: a collateral market has no live MarketState.
        MissingFeedError: no feed prices an underlying.
    """
    active = resolve_deployment(deployment)
    cparams: dict[str, LiqwidRecord] = {}
    collateral_markets: dict[str, MarketComponents] = {}
    underlyings = [borrow.market.underlying]
    for unit in units:
        record = borrow.collateral_params.get(unit)
        datum = record.datum if record is not None else None
        if record is None or not isinstance(datum, CollateralParamsDatum):
            raise UnsupportedCollateralError(unit)
        market = _collateral_market(unit, datum, active)
        underlying = datum.underlying or (market.underlying if market else "")
        if market is None or not underlying:
            raise UnsupportedCollateralError(unit)
        cparams[unit] = record
        collateral_markets.setdefault(market.market_id, market)
        underlyings.append(underlying)
    states = {borrow.market.market_id: borrow.state}
    others = [m for m_id, m in collateral_markets.items() if m_id not in states]
    if others:
        selectors = [s for market in others for s in state_selectors(active, market)]
        records = fetch_records(backend, selectors, deployment=deployment)
        for market in others:
            states[market.market_id] = _newest(
                records,
                LiqwidKind.MARKET_STATE,
                market,
            )
    feeds = resolve_feeds(
        backend,
        list(dict.fromkeys(underlyings)),
        deployment=deployment,
    )
    return HealthInputs(cparams=cparams, states=states, feeds=feeds)


def resolve_now_ms(backend: AbstractBackend) -> int:
    """The chain tip's POSIX time in milliseconds (the start of its slot)."""
    return slot_to_posix_ms(current_slot(backend))
