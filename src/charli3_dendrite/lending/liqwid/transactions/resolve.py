"""Live resolution of what a Liqwid V2 transaction spends and references.

Each resolver reads current chain state through a backend and returns resolved
:class:`~charli3_dendrite.lending.transactions.utxos.Utxo` s or decoded records:

- :func:`resolve_script_refs`: the reference-script UTxOs of a market's action
  validator, qToken policy, loan validator, loan-token policy and the two staking
  validators, from the out-refs the deployment records for their hashes, and
  :func:`resolve_loan_script_refs` the three an owner loan flow runs;
- :func:`resolve_action_utxos` and :func:`choose_action_utxo`: the market's live
  action UTxOs and the choice of the one a transaction spends, and
  :func:`resolve_flow_action` that choice for one flow's deltas;
- :func:`resolve_market_records`: each market's newest MarketParams, MarketState
  and CollateralParams records, from one fetch (:func:`resolve_market` for one
  market);
- :func:`resolve_loan`: a loan by its out-ref;
- :func:`resolve_feeds` and :func:`resolve_health_inputs`: the oracle feeds,
  CollateralParams and MarketStates a health check reads;
- :func:`resolve_now_ms`: the chain tip's time, and :func:`resolve_valid_to_ms` a
  flow's upper validity bound from it;
- :func:`resolve_owner_funding`: the wallet UTxOs that fund a flow.

:func:`loan_script_refs`, :func:`action_record`, :func:`market_records`,
:func:`loan_record`, :func:`feeds_for` and :func:`health_inputs` are the same
selections over records, UTxOs and feeds already in hand (a captured
transaction's, for instance); the resolvers read the chain and apply them.

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
from charli3_dendrite.lending.fetch import unspent_utxos
from charli3_dendrite.lending.liqwid.datums import MARKET_PARAMS_LENGTHS
from charli3_dendrite.lending.liqwid.datums import POLICY_KEY_PREFIX
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.deployment import LEGACY_GENERATION
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import StakingPair
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.deployment import staking_roles
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
from charli3_dendrite.lending.liqwid.indexing import LiqwidSelector
from charli3_dendrite.lending.liqwid.indexing import action_selector
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.indexing import newest_first
from charli3_dendrite.lending.liqwid.indexing import newest_record
from charli3_dendrite.lending.liqwid.indexing import param_selector
from charli3_dendrite.lending.liqwid.indexing import parse_action
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.indexing import state_selectors
from charli3_dendrite.lending.liqwid.lifecycle import collateral_units
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
from charli3_dendrite.lending.liqwid.transactions.terms import check_action_limits
from charli3_dendrite.lending.liqwid.transactions.terms import valid_to_ms
from charli3_dendrite.lending.transactions.common import out_ref_of
from charli3_dendrite.lending.transactions.infra import current_slot
from charli3_dendrite.lending.transactions.resolve import resolve_funding
from charli3_dendrite.lending.transactions.resolve import resolve_utxo_by_outref
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.lending.units import out_ref_key
from charli3_dendrite.lending.units import out_ref_text
from charli3_dendrite.lending.units import policy_of
from charli3_dendrite.lending.units import script_credential
from charli3_dendrite.lending.units import sole_token_name
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    import random

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
    from charli3_dendrite.lending.liqwid.transactions.terms import Flow

__all__ = [
    "ActionUtxo",
    "HealthInputs",
    "LiqwidScriptRefs",
    "LoanRecord",
    "LoanScriptRefs",
    "MarketRecords",
    "action_record",
    "choose_action_utxo",
    "collateral_link",
    "collateral_params_records",
    "feeds_for",
    "health_inputs",
    "loan_record",
    "loan_script_refs",
    "market_records",
    "record_from_utxo",
    "resolve_action_utxos",
    "resolve_feeds",
    "resolve_flow_action",
    "resolve_health_inputs",
    "resolve_loan",
    "resolve_loan_script_refs",
    "resolve_market",
    "resolve_market_records",
    "resolve_now_ms",
    "resolve_owner_funding",
    "resolve_script_refs",
    "resolve_valid_to_ms",
]

# The MarketParams layout that carries the loan validator (p[35]) and the staking
# pair (p[34]); loan flows need both. It is the longest known layout (list36).
_LOAN_LAYOUT = max(MARKET_PARAMS_LENGTHS)


# --- reference scripts -------------------------------------------------------


@dataclass(frozen=True)
class LiqwidScriptRefs:
    """The reference-script UTxOs of one market's scripts.

    ``action`` carries the action validator its MarketParams names (``p[20]``) and
    ``qtoken_policy`` the qToken minting policy. Either, and the loan scripts
    (``p[35]``, the current loan-token policy and the two ``p[34]`` staking
    validators by role), is None when it was not resolved: a transaction rebuilt
    from a capture holds only the scripts it runs.
    """

    action: Utxo | None
    qtoken_policy: Utxo | None
    loan_validator: Utxo | None
    loan_policy: Utxo | None
    borrow_staking: Utxo | None
    liquidation_staking: Utxo | None


@dataclass(frozen=True)
class LoanScriptRefs:
    """The reference-script UTxOs an owner loan flow runs on one market.

    ``loan_validator`` carries the loan validator ``p[35]`` names, ``loan_policy``
    the market's current loan-token policy and ``borrow_staking`` the borrow role of
    the ``p[34]`` staking pair. A flow that spends no action UTxO (a collateral
    change, the zero-debt close) runs these alone.
    """

    loan_validator: Utxo
    loan_policy: Utxo
    borrow_staking: Utxo


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
    tx_hash, index = out_ref_key(ref.out_ref)
    try:
        utxo = resolve_utxo_by_outref(backend, tx_hash, index)
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


def _loan_scripts(
    market: MarketComponents,
    params: MarketParamsDatum,
) -> tuple[str, str, StakingPair]:
    """The loan validator, loan-token policy and staking pair a loan flow runs.

    Raises:
        UnsupportedMarketError: the layout is shorter than 36 fields, the market has
            no current loan-token policy, or the staking pair is not one script of
            each role.
    """
    if params.length < _LOAN_LAYOUT or params.loan_validator is None:
        raise UnsupportedMarketError(
            market.name,
            f"loan flows need the {_LOAN_LAYOUT}-field MarketParams "
            f"(got {params.length})",
        )
    if market.new_borrow_policy is None:
        raise UnsupportedMarketError(market.name, "no current loan-token policy")
    try:
        roles = staking_roles(market, params)
    except ValueError as exc:
        raise UnsupportedMarketError(market.name, str(exc)) from None
    return params.loan_validator, market.new_borrow_policy, roles


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
    the action validator and the qToken policy (supply and redeem). A market unfit
    for the requested scripts is refused before the backend is read.

    Raises:
        UnsupportedMarketError: ``params`` names no action validator; with
            ``loans``, its layout is shorter than 36 fields, its staking pair is not
            one script of each role, or the market has no current loan-token
            policy; or a reference UTxO fails the checks above.
    """
    active = resolve_deployment(deployment)
    if params.action_validator is None:
        raise UnsupportedMarketError(market.name, "MarketParams names no action")
    loan_scripts = _loan_scripts(market, params) if loans else None
    action = _reference_script(backend, market, active, params.action_validator)
    qtoken = _reference_script(backend, market, active, market.qtoken_policy)
    if loan_scripts is None:
        return LiqwidScriptRefs(action, qtoken, None, None, None, None)
    owner = _owner_loan_scripts(backend, market, active, loan_scripts)
    roles = loan_scripts[2]
    return LiqwidScriptRefs(
        action=action,
        qtoken_policy=qtoken,
        loan_validator=owner.loan_validator,
        loan_policy=owner.loan_policy,
        borrow_staking=owner.borrow_staking,
        liquidation_staking=_reference_script(
            backend,
            market,
            active,
            roles.liquidation,
        ),
    )


def _owner_loan_scripts(
    backend: AbstractBackend,
    market: MarketComponents,
    deployment: LiqwidDeployment,
    loan_scripts: tuple[str, str, StakingPair],
) -> LoanScriptRefs:
    loan_validator, loan_policy, roles = loan_scripts
    return LoanScriptRefs(
        loan_validator=_reference_script(backend, market, deployment, loan_validator),
        loan_policy=_reference_script(backend, market, deployment, loan_policy),
        borrow_staking=_reference_script(backend, market, deployment, roles.borrow),
    )


def loan_script_refs(
    utxos: Sequence[Utxo],
    market: MarketComponents,
    params: MarketParamsDatum,
) -> LoanScriptRefs:
    """``market``'s owner loan scripts among ``utxos``, each found by its hash.

    The scripts are those :func:`resolve_loan_script_refs` resolves, taken from
    UTxOs already in hand (a captured transaction's reference inputs).

    Raises:
        UnsupportedMarketError: The market is unfit for loan flows (as
            :func:`resolve_loan_script_refs`).
        ValueError: No UTxO of ``utxos`` carries one of the scripts.
    """
    loan_validator, loan_policy, roles = _loan_scripts(market, params)
    held = list(utxos)
    return LoanScriptRefs(
        loan_validator=script_ref_by_hash(held, loan_validator),
        loan_policy=script_ref_by_hash(held, loan_policy),
        borrow_staking=script_ref_by_hash(held, roles.borrow),
    )


def resolve_loan_script_refs(
    backend: AbstractBackend,
    market: MarketComponents,
    params: MarketParamsDatum,
    *,
    deployment: LiqwidDeployment | None = None,
) -> LoanScriptRefs:
    """Resolve the reference-script UTxOs of ``market``'s owner loan scripts.

    The loan validator ``params`` names (``p[35]``), the market's current loan-token
    policy and the borrow staking validator of ``p[34]``, each checked as in
    :func:`resolve_script_refs`. A market unfit for loan flows is refused before the
    backend is read.

    Raises:
        UnsupportedMarketError: ``params``' layout is shorter than 36 fields, its
            staking pair is not one script of each role, the market has no current
            loan-token policy, or a reference UTxO fails the checks.
    """
    active = resolve_deployment(deployment)
    loan_scripts = _loan_scripts(market, params)
    return _owner_loan_scripts(backend, market, active, loan_scripts)


# --- action UTxOs ------------------------------------------------------------


@dataclass(frozen=True)
class ActionUtxo:
    """One of a market's action UTxOs with its decoded pending deltas."""

    utxo: Utxo
    datum: ActionDatum

    @property
    def out_ref(self) -> str:
        """The UTxO's ``<tx hash>#<index>``."""
        return out_ref_text(out_ref_of(self.utxo))

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
    ``params`` names (``p[20]``) through
    :func:`~charli3_dendrite.lending.fetch.unspent_utxos`, which pages on the rows
    the backend returns, so a page of UTxOs that are not action UTxOs does not end
    the read. A UTxO counts when it sits at that credential and
    :func:`~charli3_dendrite.lending.liqwid.indexing.parse_action` accepts it.

    Raises:
        UnsupportedMarketError: ``params`` names no action validator or the market
            has no action-token policy.
    """
    selector = action_selector(market, params)
    rows = unspent_utxos(
        backend,
        selector.address,
        assets=[selector.identity_policy],
        page_size=page_size,
    )
    found = [
        record
        for info in rows
        if (record := action_record(info, market, selector)) is not None
    ]
    return sorted(found, key=lambda action: out_ref_key(action.out_ref))


def action_record(
    info: PoolStateInfo,
    market: MarketComponents,
    selector: LiqwidSelector,
) -> ActionUtxo | None:
    """The action UTxO of ``market`` that ``info`` holds, or None.

    ``info`` counts when it sits at ``selector``'s payment credential (the action
    validator of :func:`~charli3_dendrite.lending.liqwid.indexing.action_selector`)
    and :func:`~charli3_dendrite.lending.liqwid.indexing.parse_action` accepts it.
    """
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


def resolve_flow_action(
    backend: AbstractBackend,
    records: MarketRecords,
    *,
    flow: Flow,
    deltas: ActionDeltas,
    pin: str | None = None,
    exclude: Collection[str] = (),
    rng: random.Random | None = None,
) -> ActionUtxo:
    """The live action UTxO of ``records``' market that ``flow`` spends.

    :func:`choose_action_utxo` over :func:`resolve_action_utxos`, where a
    candidate is eligible when the per-UTxO limits of ``flow`` with ``deltas``
    (`check_action_limits`: caps, liquidity) admit it. The market-level limits are
    the caller's to check first.

    Raises:
        ActionUtxoSpentError: The pin is not live or is excluded.
        LiqwidBuildError: The pin fails the limits, or no action UTxO passes them.
    """
    market = records.market
    params, state = records.params_datum, records.state_datum

    def check(candidate: ActionUtxo) -> None:
        check_action_limits(
            flow,
            params=params,
            state=state,
            action_datum=candidate.datum,
            deltas=deltas,
            action_underlying=candidate.underlying_amount(market.underlying),
            underlying=market.underlying,
        )

    return choose_action_utxo(
        resolve_action_utxos(backend, market, params),
        check=check,
        pin=pin,
        exclude=exclude,
        rng=rng,
    )


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


def market_records(
    records: Iterable[LiqwidRecord],
    market: MarketComponents,
) -> MarketRecords:
    """``market``'s newest MarketParams, MarketState and CollateralParams records.

    The records are chosen among ``records``; newest is the latest chain position,
    and the CollateralParams are :func:`collateral_params_records`'.

    Raises:
        UnsupportedMarketError: ``records`` hold no MarketParams or MarketState of
            the market.
    """
    held = list(records)
    return MarketRecords(
        market=market,
        params=_newest(held, LiqwidKind.MARKET_PARAMS, market),
        state=_newest(held, LiqwidKind.MARKET_STATE, market),
        collateral_params=collateral_params_records(held, market),
    )


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
    record = newest_record(records, kind, market.market_id)
    if record is None:
        raise UnsupportedMarketError(market.name, f"no live {kind.value} record")
    return record


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
    return {market.name: market_records(records, market) for market in wanted}


def resolve_market(
    backend: AbstractBackend,
    market: MarketComponents,
    *,
    deployment: LiqwidDeployment | None = None,
) -> MarketRecords:
    """The live records of ``market`` alone (:func:`resolve_market_records`).

    Raises:
        UnsupportedMarketError: The market has no live MarketParams or MarketState.
    """
    return resolve_market_records(backend, [market], deployment=deployment)[market.name]


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

    @property
    def collateral(self) -> dict[str, int]:
        """Collateral unit -> quantity: every held unit but lovelace and loan tokens.

        A qToken is its bare 56-hex policy (empty asset name), as a CollateralParams
        record names it.
        """
        return dict(
            collateral_units(utxo_to_info(self.utxo), self.market.loan_token_policies),
        )


def loan_record(
    utxo: Utxo,
    *,
    deployment: LiqwidDeployment | None = None,
) -> LoanRecord:
    """The current-generation loan ``utxo`` holds.

    Raises:
        UnsupportedLoanError: ``utxo`` is a legacy-generation loan (at a legacy loan
            validator or holding a legacy loan token) or is no loan at all.
    """
    active = resolve_deployment(deployment)
    out_ref = out_ref_text(out_ref_of(utxo))
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
    tx_hash, index = out_ref_key(out_ref)
    utxo = resolve_utxo_by_outref(backend, tx_hash, index, allow_spent=allow_spent)
    return loan_record(utxo, deployment=deployment)


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
    return feeds_for(assets, _live_feeds(backend, deployment))


def _live_feeds(
    backend: AbstractBackend,
    deployment: LiqwidDeployment | None,
) -> list[LiqwidFeed]:
    selectors = [
        selector
        for selector in liqwid_selectors(deployment)
        if selector.kind is LiqwidKind.ORACLE
    ]
    return [
        feed
        for record in fetch_records(backend, selectors, deployment=deployment)
        if (feed := parse_feed(record.info, deployment)) is not None
    ]


def feeds_for(
    assets: Iterable[str],
    feeds: Iterable[LiqwidFeed],
) -> dict[str, LiqwidFeed]:
    """The feed among ``feeds`` pricing each of ``assets``, keyed by the asset.

    The freshest feed per priced asset is kept
    (:func:`~charli3_dendrite.lending.liqwid.oracles.select_feeds`); an asset takes
    its exact feed when there is one, else the policy-wide feed of its policy.

    Raises:
        MissingFeedError: no feed prices an asset.
    """
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


def collateral_link(
    unit: str,
    record: LiqwidRecord | None,
    deployment: LiqwidDeployment,
) -> tuple[MarketComponents, CollateralParamsDatum]:
    """The collateral market of ``unit`` and the CollateralParams datum naming it.

    ``record`` is the borrow market's exact CollateralParams record of ``unit``. Its
    datum links the collateral market by that market's MarketState NFT and names
    the market's underlying; a health check reads the linked MarketState and the
    underlying's feed.

    Raises:
        UnsupportedCollateralError: ``record`` is None, not an exact
            CollateralParams record, names no collateral market or underlying, or
            links a market ``deployment`` does not list.
    """
    datum = record.datum if record is not None else None
    if (
        not isinstance(datum, CollateralParamsDatum)
        or datum.policy_wide
        or not datum.underlying
        or not datum.collateral_market_state
    ):
        raise UnsupportedCollateralError(unit)
    market = deployment.market_of_state_policy(
        policy_of(datum.collateral_market_state),
    )
    if market is None:
        raise UnsupportedCollateralError(unit)
    return market, datum


@dataclass(frozen=True)
class _CollateralLinks:
    """Each unit's CollateralParams record, the collateral markets, the underlyings.

    The underlyings start with the borrowed one; markets are keyed by id.
    """

    cparams: dict[str, LiqwidRecord]
    markets: dict[str, MarketComponents]
    underlyings: list[str]


def _collateral_links(
    units: Iterable[str],
    borrow: MarketRecords,
    deployment: LiqwidDeployment,
) -> _CollateralLinks:
    """Link each of ``units`` to its collateral market (:func:`collateral_link`).

    Raises:
        UnsupportedCollateralError: As :func:`collateral_link`.
    """
    cparams: dict[str, LiqwidRecord] = {}
    markets: dict[str, MarketComponents] = {}
    underlyings = [borrow.market.underlying]
    for unit in units:
        market, datum = collateral_link(
            unit,
            borrow.collateral_params.get(unit),
            deployment,
        )
        cparams[unit] = borrow.collateral_params[unit]
        markets.setdefault(market.market_id, market)
        underlyings.append(datum.underlying)
    return _CollateralLinks(cparams, markets, list(dict.fromkeys(underlyings)))


def _linked_health_inputs(
    links: _CollateralLinks,
    *,
    borrow: MarketRecords,
    records: Iterable[LiqwidRecord],
    feeds: Iterable[LiqwidFeed],
) -> HealthInputs:
    """:func:`health_inputs` over collateral links already made.

    Raises:
        UnsupportedMarketError: ``records`` hold no MarketState of a collateral
            market.
        MissingFeedError: no feed prices an underlying.
    """
    held = list(records)
    states = {borrow.market.market_id: borrow.state}
    for market_id, market in links.markets.items():
        if market_id not in states:
            states[market_id] = _newest(held, LiqwidKind.MARKET_STATE, market)
    return HealthInputs(
        cparams=links.cparams,
        states=states,
        feeds=feeds_for(links.underlyings, feeds),
    )


def health_inputs(
    units: Iterable[str],
    *,
    borrow: MarketRecords,
    records: Iterable[LiqwidRecord],
    feeds: Iterable[LiqwidFeed],
    deployment: LiqwidDeployment | None = None,
) -> HealthInputs:
    """The CollateralParams, MarketStates and feeds a health check of ``units`` reads.

    ``borrow`` is the borrow market's records, whose CollateralParams price the
    units; each other collateral market's MarketState is its newest in ``records``
    and each underlying's feed is :func:`feeds_for`'s choice among ``feeds``. A
    collateral of the borrow market itself (a wash borrow) shares its MarketState
    and feed.

    Raises:
        UnsupportedCollateralError: a unit has no exact CollateralParams record of
            the borrow market, or the record names no collateral market or
            underlying (:func:`collateral_link`).
        UnsupportedMarketError: ``records`` hold no MarketState of a collateral
            market.
        MissingFeedError: no feed prices an underlying.
    """
    active = resolve_deployment(deployment)
    return _linked_health_inputs(
        _collateral_links(units, borrow, active),
        borrow=borrow,
        records=records,
        feeds=feeds,
    )


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
    market's records, whose CollateralParams price them. The live MarketStates of
    the other collateral markets and the live feeds are read, then
    :func:`health_inputs` selects from them.

    Raises:
        UnsupportedCollateralError: a unit has no exact CollateralParams record of
            the borrow market, or the record names no collateral market or
            underlying (:func:`collateral_link`).
        UnsupportedMarketError: a collateral market has no live MarketState.
        MissingFeedError: no feed prices an underlying.
    """
    active = resolve_deployment(deployment)
    links = _collateral_links(units, borrow, active)
    others = [m for m_id, m in links.markets.items() if m_id != borrow.market.market_id]
    selectors = [s for market in others for s in state_selectors(active, market)]
    records = (
        fetch_records(backend, selectors, deployment=deployment) if selectors else []
    )
    return _linked_health_inputs(
        links,
        borrow=borrow,
        records=records,
        feeds=_live_feeds(backend, deployment),
    )


def resolve_now_ms(backend: AbstractBackend) -> int:
    """The chain tip's POSIX time in milliseconds (the start of its slot)."""
    return slot_to_posix_ms(current_slot(backend))


def resolve_valid_to_ms(
    backend: AbstractBackend,
    records: MarketRecords,
    requested_ms: int | None,
) -> int:
    """The upper validity bound of a flow on ``records``' market, from the tip.

    ``requested_ms``, or the default window, as `valid_to_ms` bounds it by the
    market's batch deadline.

    Raises:
        ValueError: ``requested_ms`` is not after the chain tip.
        MarketAwaitingBatchError: The batch deadline is too close.
    """
    return valid_to_ms(
        resolve_now_ms(backend),
        requested_ms,
        state=records.state_datum,
        params=records.params_datum,
        market=records.market.name,
    )


def resolve_owner_funding(
    backend: AbstractBackend,
    owner_address: str,
    funding: Sequence[Utxo] | None = None,
    *,
    holding: str | None = None,
) -> list[Utxo]:
    """The wallet UTxOs that fund a flow: ``funding``, else those at the address.

    The address's UTxOs are its most recent ones (`resolve_funding`); with
    ``holding`` (an asset unit) every unspent UTxO at the address holding that
    asset is added after them, each out-ref once. ``funding``, when given, is
    used as it is.

    The funding is the set of wallet inputs a flow starts from, not a balanced
    selection: only a redeem reads its qTokens from it (through ``holding``). What
    the other flows take from the wallet (the underlying of a supply or a repay,
    the qTokens a loan locks) and the fee are left to the caller's balancer, which
    adds wallet inputs when the funding falls short.
    """
    if funding is not None:
        return list(funding)
    wallet = resolve_funding(backend, owner_address)
    if holding is None:
        return wallet
    seen = {u.out_ref for u in wallet}
    held = resolve_funding(backend, owner_address, limit=None, asset=holding)
    return wallet + [u for u in held if u.out_ref not in seen]
