"""Classify and decode raw Liqwid V2 UTxO records.

:func:`liqwid_selectors` lists every credential worth watching with the token a
genuine record there holds: view selectors (market states and loans, which become
views) and context selectors (market and collateral parameters and oracle feeds,
which views depend on). :func:`parse_utxo` turns one UTxO record into its typed
record, or None. Neither function touches a backend.

Action, Batch and BatchFinal validators belong to the deployment but hold nothing a
view reads, so they are not selectors.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from functools import lru_cache
from typing import Union

import cbor2  # type: ignore[import-not-found]

from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.fluidtokens_v4.indexing import DECODE_ERRORS
from charli3_dendrite.lending.fluidtokens_v4.indexing import script_credential
from charli3_dendrite.lending.fluidtokens_v4.state import names_under
from charli3_dendrite.lending.liqwid.datums import COLLATERAL_PARAMS_LENGTHS
from charli3_dendrite.lending.liqwid.datums import LOAN_LENGTHS
from charli3_dendrite.lending.liqwid.datums import MARKET_PARAMS_LENGTHS
from charli3_dendrite.lending.liqwid.datums import MARKET_STATE_LENGTHS
from charli3_dendrite.lending.liqwid.datums import ORACLE_FEED_LENGTHS
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import OracleFeedDatum
from charli3_dendrite.lending.liqwid.datums import decode_collateral_params
from charli3_dendrite.lending.liqwid.datums import decode_loan
from charli3_dendrite.lending.liqwid.datums import decode_market_params
from charli3_dendrite.lending.liqwid.datums import decode_market_state
from charli3_dendrite.lending.liqwid.datums import decode_oracle_feed
from charli3_dendrite.lending.liqwid.deployment import LEGACY_GENERATION
from charli3_dendrite.lending.liqwid.deployment import NEW_GENERATION
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.markets import CONTEXT_ROLE
from charli3_dendrite.lending.markets import VIEW_ROLE
from charli3_dendrite.lending.markets import WatchSpec
from charli3_dendrite.lending.units import plutus_fields
from charli3_dendrite.lending.units import script_payment_address

LiqwidDatum = Union[
    MarketStateDatum,
    MarketParamsDatum,
    CollateralParamsDatum,
    LoanDatum,
    OracleFeedDatum,
]


class LiqwidKind(str, Enum):
    """The kinds of Liqwid V2 record a reader decodes."""

    MARKET_STATE = "market_state"
    LOAN = "loan"
    MARKET_PARAMS = "market_params"
    COLLATERAL_PARAMS = "collateral_params"
    ORACLE = "oracle"


# Top-level field counts of each kind's records. Anything else that holds a genuine
# identity token (a parked ``C0[]``, an emptied or foreign-shaped state) is not a
# record.
_LENGTHS: dict[LiqwidKind, frozenset[int]] = {
    LiqwidKind.MARKET_STATE: MARKET_STATE_LENGTHS,
    LiqwidKind.LOAN: LOAN_LENGTHS,
    LiqwidKind.MARKET_PARAMS: MARKET_PARAMS_LENGTHS,
    LiqwidKind.COLLATERAL_PARAMS: COLLATERAL_PARAMS_LENGTHS,
    LiqwidKind.ORACLE: ORACLE_FEED_LENGTHS,
}

_DECODERS: dict[LiqwidKind, Callable[[bytes], LiqwidDatum]] = {
    LiqwidKind.MARKET_STATE: decode_market_state,
    LiqwidKind.LOAN: decode_loan,
    LiqwidKind.MARKET_PARAMS: decode_market_params,
    LiqwidKind.COLLATERAL_PARAMS: decode_collateral_params,
    LiqwidKind.ORACLE: decode_oracle_feed,
}

# Loan tokens carry a state marker as their name (it flips on every modification);
# the market NFTs and the oracle token have an empty name.
_NAMED_IDENTITY = frozenset({LiqwidKind.LOAN})


@dataclass(frozen=True)
class LiqwidSelector:
    """Where one Liqwid record kind lives, and the token every genuine one holds.

    ``role`` is ``VIEW_ROLE`` for records that become views (market states, loans)
    and ``CONTEXT_ROLE`` for records views depend on (parameters, oracle feeds).
    ``market_id`` is the market's MarketState NFT policy; it is None for oracle
    feeds, which belong to no market.
    """

    kind: LiqwidKind
    payment_credential: str
    identity_policy: str
    role: str
    market_id: str | None

    @property
    def address(self) -> str:
        """Enterprise address with this selector's payment credential."""
        return script_payment_address(self.payment_credential)

    def watch_spec(self) -> WatchSpec:
        """This selector as a protocol-agnostic watch specification."""
        return WatchSpec(
            payment_credential=self.payment_credential,
            identity_policy=self.identity_policy,
            role=self.role,
        )


@dataclass(frozen=True)
class LiqwidRecord:
    """One genuine Liqwid record, decoded.

    ``generation`` is ``"legacy"`` or ``"new"`` for a loan (the generation of the
    validator it sits at) and None for every other kind. ``info`` is the UTxO the
    record was read from; it takes no part in equality or hashing.
    """

    kind: LiqwidKind
    out_ref: str
    market_id: str | None
    generation: str | None
    datum: LiqwidDatum
    info: PoolStateInfo = field(hash=False, compare=False)


def _param_selector(
    deployment: LiqwidDeployment,
    kind: LiqwidKind,
    market: MarketComponents,
) -> LiqwidSelector:
    """The parameter-address selector of one market's MarketParams or CollateralParams.

    Both kinds share the parameter credential; the market's NFT policy for the kind
    tells them, and the markets, apart.
    """
    policy = (
        market.market_params_policy
        if kind is LiqwidKind.MARKET_PARAMS
        else market.collateral_params_policy
    )
    return LiqwidSelector(
        kind=kind,
        payment_credential=deployment.param_credential,
        identity_policy=policy,
        role=CONTEXT_ROLE,
        market_id=market.market_id,
    )


def _routes(
    deployment: LiqwidDeployment,
) -> tuple[tuple[LiqwidSelector, str | None], ...]:
    """Every selector with its loan generation (None outside loans), views first."""
    states: list[tuple[LiqwidSelector, str | None]] = [
        (
            LiqwidSelector(
                kind=LiqwidKind.MARKET_STATE,
                payment_credential=validator,
                identity_policy=market.market_state_policy,
                role=VIEW_ROLE,
                market_id=market.market_id,
            ),
            None,
        )
        for market in deployment.markets
        for validator in market.market_state_validators
    ]
    loans: list[tuple[LiqwidSelector, str | None]] = []
    for market in deployment.markets:
        generations = (
            (LEGACY_GENERATION, market.legacy_loan_validators, market.borrow_policy),
            (NEW_GENERATION, market.new_loan_validators, market.new_borrow_policy),
        )
        for generation, validators, policy in generations:
            if policy is None:
                continue
            loans.extend(
                (
                    LiqwidSelector(
                        kind=LiqwidKind.LOAN,
                        payment_credential=validator,
                        identity_policy=policy,
                        role=VIEW_ROLE,
                        market_id=market.market_id,
                    ),
                    generation,
                )
                for validator in validators
            )
    params = [
        (_param_selector(deployment, LiqwidKind.MARKET_PARAMS, market), None)
        for market in deployment.markets
    ]
    collateral = [
        (_param_selector(deployment, LiqwidKind.COLLATERAL_PARAMS, market), None)
        for market in deployment.markets
    ]
    oracles = [
        (
            LiqwidSelector(
                kind=LiqwidKind.ORACLE,
                payment_credential=validator.script_hash,
                identity_policy=deployment.oracle_token_policy,
                role=CONTEXT_ROLE,
                market_id=None,
            ),
            None,
        )
        for validator in deployment.oracle_validators
    ]
    return (*states, *loans, *params, *collateral, *oracles)


@lru_cache(maxsize=8)
def _route_index(
    deployment: LiqwidDeployment,
) -> Mapping[str, tuple[tuple[LiqwidSelector, str | None], ...]]:
    """Routes grouped by payment credential (the parameter credential has many)."""
    index: dict[str, list[tuple[LiqwidSelector, str | None]]] = {}
    for route in _routes(deployment):
        index.setdefault(route[0].payment_credential, []).append(route)
    return {credential: tuple(routes) for credential, routes in index.items()}


@lru_cache(maxsize=1)
def _default_route_index() -> (
    Mapping[str, tuple[tuple[LiqwidSelector, str | None], ...]]
):
    """The route index of the bundled deployment, built once."""
    return _route_index(default_deployment())


def liqwid_selectors(
    deployment: LiqwidDeployment | None = None,
) -> tuple[LiqwidSelector, ...]:
    """Every Liqwid V2 selector of ``deployment`` (default: the bundled snapshot).

    View selectors come first: each MarketState validator with its market's NFT
    policy, then each loan validator with its market's legacy or current loan-token
    policy. Context selectors follow: the parameter credential once per MarketParams
    NFT policy and once per CollateralParams NFT policy, then each oracle validator
    with the oracle token policy. The order is deterministic.
    """
    active = deployment if deployment is not None else default_deployment()
    return tuple(selector for selector, _ in _routes(active))


def _holds_identity(info: PoolStateInfo, selector: LiqwidSelector) -> bool:
    """Whether ``info`` holds exactly one token under the selector's identity policy.

    The token must have quantity 1, and an empty name unless it is a loan token.
    """
    names = names_under(info.assets, selector.identity_policy)
    if len(names) != 1:
        return False
    if info.assets.root[selector.identity_policy + names[0]] != 1:
        return False
    return selector.kind in _NAMED_IDENTITY or names[0] == ""


def _decode(kind: LiqwidKind, datum_cbor: str) -> LiqwidDatum | None:
    """``kind``'s record from an inline datum, or None when it is not one.

    The top-level field count is checked first, so a parked ``C0[]`` or a datum of
    another kind's shape is rejected by shape. A datum of the right shape whose
    fields do not decode (truncated, not CBOR, nested past the recursion limit, a
    decimal-fraction or bigfloat tag whose exponent overflows, a wrong-typed field)
    is rejected too.
    """
    try:
        raw = bytes.fromhex(datum_cbor)
        if len(plutus_fields(cbor2.loads(raw))) not in _LENGTHS[kind]:
            return None
        return _DECODERS[kind](raw)
    except (*DECODE_ERRORS, RecursionError, ArithmeticError):
        return None


def parse_utxo(
    info: PoolStateInfo,
    deployment: LiqwidDeployment | None = None,
) -> LiqwidRecord | None:
    """Classify one UTxO record and decode it into its Liqwid V2 record.

    Returns None, never raising, when the record is not genuine: its payment
    credential is no selector's, it holds no identity token of a selector at that
    credential (or tokens of more than one), it carries no inline datum, or its
    datum is not a record of its kind (wrong shape, malformed CBOR, wrong-typed
    fields). Anyone can send a UTxO to a script address, so the identity token,
    not the address, makes a record genuine: dust at the parameter address, the
    token-less oracle decoys and parameter NFTs of a retired policy all return
    None. A MarketState is accepted at any of its market's state validators.
    """
    index = _default_route_index() if deployment is None else _route_index(deployment)
    credential = script_credential(info.address)
    routes = index.get(credential, ()) if credential is not None else ()
    matched = [route for route in routes if _holds_identity(info, route[0])]
    if len(matched) != 1 or not info.datum_cbor:
        return None
    selector, generation = matched[0]
    datum = _decode(selector.kind, info.datum_cbor)
    if datum is None:
        return None
    return LiqwidRecord(
        kind=selector.kind,
        out_ref=f"{info.tx_hash}#{info.tx_index}",
        market_id=selector.market_id,
        generation=generation,
        datum=datum,
        info=info,
    )
