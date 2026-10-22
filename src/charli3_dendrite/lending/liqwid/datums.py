"""Liqwid Finance datum parsers.

Pure decoders for the Liqwid V2 on-chain records. The first five functions read
single values for price and rate lookups: the Oracle V2 price feed, the market state
(qToken exchange rate), and the market parameters (risk configuration); each reads
its value through the matching record decoder. The ``decode_*`` functions decode
whole records into frozen dataclasses: market state, market parameters (every
historical layout), collateral parameters, loan, oracle feed and action. Each parser
takes the datum as CBOR hex or raw bytes.

Liqwid records are positional field lists, written both as definite and as
indefinite CBOR arrays; the record decoders read either encoding identically. A
record of the wrong length, a field of the wrong type, or bytes that are not CBOR
raise ``ValueError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Callable
from typing import Final
from typing import TypeVar

import cbor2  # type: ignore[import-not-found]

from charli3_dendrite.lending.units import DECODE_ERRORS
from charli3_dendrite.lending.units import asset_unit
from charli3_dendrite.lending.units import constr
from charli3_dendrite.lending.units import plutus_fields


def parse_liqwid_oracle_v2(datum: str | bytes) -> tuple[str, tuple[int, int]]:
    """Liqwid Oracle V2 datum -> (priced_asset_key, (num, denom)).

    The ``asset_key`` and price of :func:`decode_oracle_feed`; the rate is the priced
    asset in the feed's configured quote. ADA (the empty TupleAsset) is
    ``"lovelace"``, an exact asset its unit, and a policy-wide feed
    (``Constr0[policy]``) ``"policy:<policy>"`` (:func:`feed_asset_key`).

    Raises ValueError on anything ``decode_oracle_feed`` rejects, a non-positive
    denominator included.
    """
    feed = decode_oracle_feed(datum)
    return feed.asset_key, feed.price


def liqwid_oracle_feed_id(datum: str | bytes) -> str:
    """Liqwid Oracle V2 datum field 0 -- the oracle updater's key -- as hex.

    The updater key is data, not a feed identity: one key signs the feeds of many
    assets, and the key rotates over time. At every slot each priced asset has
    exactly one unspent feed that carries the oracle token, so the priced asset
    (field 1, see ``decode_oracle_feed``) identifies a feed.

    Raises ValueError on anything ``decode_oracle_feed`` rejects.
    """
    return decode_oracle_feed(datum).updater


def parse_liqwid_market_state(datum: str | bytes) -> tuple[int, int]:
    """Liqwid market-state datum -> qToken exchange rate (state field 9).

    The ``qtoken_rate`` of :func:`decode_market_state` (underlying per qToken), as
    (num, denom).

    Raises ValueError on a non-positive rate and on anything ``decode_market_state``
    rejects.
    """
    num, denom = decode_market_state(datum).qtoken_rate
    if num <= 0:
        raise ValueError("non-positive liqwid market exchange rate")
    return (num, denom)


def parse_liqwid_market_param(
    datum: str | bytes,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Liqwid market-parameter (config) datum -> (initial_qtoken_rate, max_ltv).

    This datum is the market's interest-model and risk configuration, not a price
    source (the live qToken exchange rate is read from the market-state datum, see
    ``parse_liqwid_market_state``). Two rationals of :func:`decode_market_params`
    are surfaced, both as (num, denom):

    * field 0, the initial qToken exchange rate (underlying per qToken) a new market
      starts from, e.g. 1/50;
    * field 12, the maximum loan-to-value on weighted collateral; the protocol's
      minimum health factor is its reciprocal.

    Raises ValueError on anything ``decode_market_params`` rejects.
    """
    params = decode_market_params(datum)
    return (params.initial_qtoken_rate, params.max_ltv)


def liqwid_qtoken_rate(
    *,
    qtoken_rate_num: int,
    qtoken_rate_denom: int,
) -> tuple[int, int]:
    """QToken -> underlying rate as (num, denom). Raises on non-positive denom/num."""
    if qtoken_rate_num <= 0 or qtoken_rate_denom <= 0:
        raise ValueError("non-positive qtoken rate")
    return (qtoken_rate_num, qtoken_rate_denom)


# --- Whole-record decoders -------------------------------------------------------

Rational = tuple[int, int]
"""A Plutus rational ``[numerator, denominator]`` (denominator > 0)."""

# The accepted field-list lengths of each record. A length outside a record's set is
# a layout this module does not know.
MARKET_PARAMS_LENGTHS: Final[frozenset[int]] = frozenset({27, 30, 32, 34, 36})
"""Every MarketParams layout since launch: list27 -> 30 -> 32 -> 34 -> 36.

The layouts are append-only, so fields 0-26 sit at the same positions in all five.
"""
MARKET_STATE_LENGTHS: Final[frozenset[int]] = frozenset({11})
COLLATERAL_PARAMS_LENGTHS: Final[frozenset[int]] = frozenset({5})
LOAN_LENGTHS: Final[frozenset[int]] = frozenset({5})
ORACLE_FEED_LENGTHS: Final[frozenset[int]] = frozenset({5, 6})
"""A list5 feed, or a list6 feed that adds field 5 (``OracleFeedDatum.caps``)."""
ACTION_LENGTHS: Final[frozenset[int]] = frozenset({2})
POLICY_KEY_PREFIX: Final[str] = "policy:"
"""Prefix of a policy-wide feed's asset key (see :func:`feed_asset_key`)."""

LIQWID_DECODE_ERRORS: Final = (
    *DECODE_ERRORS,
    cbor2.CBORError,
    EOFError,
    RecursionError,
    ArithmeticError,
)
"""Every error a malformed Liqwid datum can raise while it is decoded.

The record decoders and :func:`field_count` turn each of them into a ``ValueError``,
so a caller that catches this tuple also catches anything a decoder raises.
``RecursionError``: arrays nested past the interpreter's recursion limit.
``ArithmeticError``: a constructor alternative or a decimal-fraction / bigfloat
exponent that overflows, however the cbor2 build reports it.
"""
# A parked output carries its NFT on an empty ``Constr0 []`` instead of a record.
_PARKED_CONSTR_TAG = 121

_PAIR = 2
_INCOME_RATIO_PARTS = 4
_RATE_MODEL_PARTS = 4
_ACTION_DELTAS = 5
# MarketParams positions. The caps exist from list34 on (p[30]/p[31] of list32 are
# stake-credential options, never caps); the withdraw-zero pair and the current loan
# validator exist only in list36.
_P_INITIAL_QTOKEN_RATE = 0
_P_INCOME_RATIO = 2
_P_LEGACY_LOAN_VALIDATOR = 9
_P_INTEREST_PERIOD = 10
_P_RATE_MODEL = 11
_P_MAX_LTV = 12
_P_MAX_COLLATERALS = 14
_P_MIN_INTEREST_RATE = 15
_P_CLOSE_FACTOR = 16
_P_LIQUIDATION_THRESHOLD = 18
_P_ACTION_VALIDATOR = 20
_P_BATCH_VALIDATOR = 21
_P_BORROW_CAP = 32
_P_SUPPLY_CAP = 33
_P_WITHDRAW_ZERO = 34
_P_LOAN_VALIDATOR = 35
_CAPPED_LAYOUT = 34
_CURRENT_LAYOUT = 36
# Plutus constructor alternatives: Maybe is Just = 0 / Nothing = 1; an asset class is
# Constr1[[policy, name]] (one exact asset) or Constr0[policy] (a whole policy); a
# credential is PubKey = 0 / Script = 1.
_JUST = 0
_NOTHING = 1
_POLICY_WIDE = 0
_EXACT_ASSET = 1
_SCRIPT_CREDENTIAL = 1
_ORACLE_CAPS_FIELD = 5

_T = TypeVar("_T")


@dataclass(frozen=True)
class MarketStateDatum:
    """A market's pooled state (list11, every MarketState validator since launch).

    ``reserve`` is signed. ``index`` is the cumulative interest index a loan's debt
    is scaled by; ``rate`` is the borrow rate per interest period; ``qtoken_rate`` is
    underlying per qToken. Times are POSIX milliseconds.
    """

    cash: int
    reserve: int
    qtokens: int
    principal: int
    interest: int
    index: int
    rate: Rational
    last_interest_ms: int
    last_batch_ms: int
    qtoken_rate: Rational
    min_ada: int


@dataclass(frozen=True)
class MarketParamsDatum:
    """A market's risk and interest configuration (any of the five layouts).

    ``length`` is the layout (27, 30, 32, 34 or 36). ``borrow_cap`` is the maximum
    utilization (``None`` when unset or before list34); ``supply_cap`` is an amount
    of underlying base units (``None`` when unset or before list34);
    ``withdraw_zero`` (the two staking validators a borrow or a liquidation
    withdraws zero from, in datum order) and ``loan_validator`` exist only in
    list36. Script hashes are hex. ``raw`` keeps every field as decoded.
    """

    length: int
    initial_qtoken_rate: Rational
    income_ratio: tuple[int, ...]
    legacy_loan_validator: str | None
    interest_period_ms: int
    rate_model: tuple[Rational, ...]
    max_ltv: Rational
    max_collaterals: int
    min_interest_rate: Rational
    close_factor: Rational
    liquidation_threshold: Rational
    action_validator: str | None
    batch_validator: str | None
    borrow_cap: Rational | None
    supply_cap: int | None
    withdraw_zero: tuple[str, str] | None
    loan_validator: str | None
    raw: tuple[object, ...] = field(hash=False, compare=False)


@dataclass(frozen=True)
class CollateralParamsDatum:
    """One accepted collateral of a borrow market (list5).

    ``collateral`` is the exact unit of a ``Constr1[[policy, name]]`` record (a
    qToken: its 56-hex policy, empty name) with ``policy_wide`` False, or the 56-hex
    policy of a script-class ``Constr0[policy]`` record with ``policy_wide`` True.
    ``weight`` scales the collateral's value, ``liquidation_discount`` is the raw
    seize factor (a liquidator seizes ``repay / liquidation_discount`` of value) and
    ``treasury_split`` the treasury's share. ``underlying`` is the collateral
    market's underlying unit (``"lovelace"`` for Ada) and
    ``collateral_market_state`` that market's MarketState NFT unit; both are ``""``
    when the record names no collateral market (field 4 is ``Nothing``, as on a
    script-class record).
    """

    collateral: str
    policy_wide: bool
    weight: Rational
    liquidation_discount: Rational
    treasury_split: Rational
    underlying: str
    collateral_market_state: str


@dataclass(frozen=True)
class LoanDatum:
    """A loan UTxO's record (list5, legacy and current loan validators).

    ``owner`` is the borrower's key hash (hex); ``index`` is the market interest
    index the loan was last brought to.
    """

    owner: str
    principal: int
    interest: int
    min_interest: int
    index: int


@dataclass(frozen=True)
class OracleFeedDatum:
    """An oracle price feed (list5, or list6 with field 5).

    ``asset`` is the priced asset's unit (``"lovelace"`` for Ada) with
    ``policy_wide`` False, or, for a ``Constr0[policy]`` feed, the bare 56-hex
    policy with ``policy_wide`` True (never ``"lovelace"``). ``price`` is USD per
    1,000,000 base units. ``updater`` is the publishing key (hex), which is data,
    not a feed identity. ``caps`` is field 5 as decoded, ``None`` on a list5 feed.
    """

    updater: str
    asset: str
    policy_wide: bool
    price: Rational
    ts_ms: int
    validity_ms: int
    caps: object | None = field(hash=False, compare=False)

    @property
    def asset_key(self) -> str:
        """Identity of the priced asset (:func:`feed_asset_key`)."""
        return feed_asset_key(self.asset, policy_wide=self.policy_wide)


def feed_asset_key(asset: str, *, policy_wide: bool) -> str:
    """Identity of a feed's priced asset; a policy-wide feed never reads as a unit.

    An exact feed's key is its unit (``"lovelace"`` for Ada). A policy-wide feed's
    is :data:`POLICY_KEY_PREFIX` + the 56-hex policy, so it never reads as a unit
    or as a bare policy.
    """
    return POLICY_KEY_PREFIX + asset if policy_wide else asset


@dataclass(frozen=True)
class ActionDatum:
    """An action UTxO's pending deltas, folded into the market at the next batch.

    The record is ``[[supply, qTokens, principal, interest, min_interest_paid],
    nonce]``; the deltas are signed and ``nonce`` is kept as decoded.
    """

    supply_delta: int
    qtoken_delta: int
    principal_delta: int
    interest_delta: int
    min_interest_paid: int
    nonce: object = field(hash=False, compare=False)


def _malformed(exc: BaseException, prefix: str) -> ValueError:
    """The ``ValueError`` a decode failure ``exc`` becomes, its message prefixed.

    A ``ValueError`` from outside this module (``bytes.fromhex``, a constructor tag
    that ``constr`` rejects) keeps its message behind the record prefix; any other
    failure (bytes that are not CBOR, a truncated or too deeply nested array, an
    overflowing number, a wrong-typed field) is a malformed datum.
    """
    if isinstance(exc, ValueError) and not isinstance(exc, cbor2.CBORError):
        return ValueError(f"{prefix}{exc}")
    return ValueError(f"{prefix}malformed datum")


def _load(datum: str | bytes) -> object:
    """``datum`` (CBOR hex or raw bytes) decoded by cbor2."""
    raw = bytes.fromhex(datum) if isinstance(datum, str) else bytes(datum)
    return cbor2.loads(raw)


def _decode(
    datum: str | bytes,
    lengths: frozenset[int],
    what: str,
    build: Callable[[list], _T],
) -> _T:
    """Decode ``datum`` as a bare field list of an allowed length and build a record.

    Anything that is not such a list (a constructor, bytes that are not CBOR, a
    truncated or too deeply nested array) and any wrong-typed field raise
    ``ValueError``, whose message starts with ``"liqwid <what>: "``.
    """
    prefix = f"liqwid {what}: "
    try:
        decoded = _load(datum)
        if not isinstance(decoded, list):
            msg = f"{prefix}datum is not a field list"
            raise ValueError(msg)
        fields = plutus_fields(decoded)
        if len(fields) not in lengths:
            msg = f"{prefix}unknown layout of {len(fields)} fields"
            raise ValueError(msg)
        return build(fields)
    except LIQWID_DECODE_ERRORS as exc:
        # This module's own messages already carry the prefix.
        if isinstance(exc, ValueError) and str(exc).startswith(prefix):
            raise
        raise _malformed(exc, prefix) from exc


def field_count(datum: str | bytes) -> int | None:
    """Top-level field count of a Liqwid record datum; None for a parked output.

    A parked output holds its NFT on an empty ``Constr0 []``. The count is taken
    before any layout check, so a caller can tell an unknown layout from a datum
    that is no record at all.

    Raises:
        ValueError: The datum is empty, not CBOR, nested past the recursion limit,
            or anything but a bare field list or a parked constructor. The message
            starts with ``"liqwid record: "``.
    """
    prefix = "liqwid record: "
    try:
        if not datum:
            msg = f"{prefix}no inline datum"
            raise ValueError(msg)
        decoded = _load(datum)
        if isinstance(decoded, cbor2.CBORTag):
            if decoded.tag == _PARKED_CONSTR_TAG and not decoded.value:
                return None
            msg = f"{prefix}constructor tag {decoded.tag} is not a field list"
            raise ValueError(msg)
        if not isinstance(decoded, list):
            msg = f"{prefix}{type(decoded).__name__} is not a field list"
            raise ValueError(msg)
        return len(decoded)
    except LIQWID_DECODE_ERRORS as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(prefix):
            raise
        raise _malformed(exc, prefix) from exc


def _int(value: object, what: str) -> int:
    """``value`` as an integer; ``ValueError`` for anything else (bools included)."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"liqwid {what}: expected an integer"
        raise ValueError(msg)
    return value


def _bytes(value: object, what: str) -> bytes:
    """A bytestring field; ``ValueError`` for anything else."""
    if not isinstance(value, bytes):
        msg = f"liqwid {what}: expected a bytestring"
        raise ValueError(msg)
    return value


def _hex(value: object, what: str) -> str:
    """A bytestring field as hex."""
    return _bytes(value, what).hex()


def _list(value: object, what: str, length: int | None = None) -> list:
    """A bare list field (of ``length`` items when given); ``ValueError`` otherwise."""
    if not isinstance(value, list):
        msg = f"liqwid {what}: expected a list"
        raise ValueError(msg)
    items = plutus_fields(value)
    if length is not None and len(items) != length:
        msg = f"liqwid {what}: expected {length} items, got {len(items)}"
        raise ValueError(msg)
    return items


def _rational(value: object, what: str) -> Rational:
    """A ``[num, den]`` field with ``den > 0``."""
    num, den = (_int(part, what) for part in _list(value, what, _PAIR))
    if den <= 0:
        msg = f"liqwid {what}: non-positive denominator"
        raise ValueError(msg)
    return (num, den)


def _constr(value: object, what: str) -> tuple[int, list]:
    """(alternative, fields) of a Plutus constructor; ``ValueError`` otherwise."""
    if not isinstance(value, cbor2.CBORTag):
        msg = f"liqwid {what}: expected a constructor"
        raise ValueError(msg)
    return constr(value)


def _maybe(value: object, what: str) -> object | None:
    """The payload of ``Just x`` (``Constr0[x]``), or ``None`` for ``Constr1[]``."""
    alt, fields = _constr(value, what)
    if alt == _JUST and len(fields) == 1:
        return fields[0]
    if alt == _NOTHING and not fields:
        return None
    msg = f"liqwid {what}: expected Just/Nothing"
    raise ValueError(msg)


def _asset_class(value: object, what: str) -> tuple[str, bool]:
    """(unit, policy_wide) of ``Constr1[[policy, name]]`` or ``Constr0[policy]``.

    An exact asset is its dendrite unit (``"lovelace"`` for ``[b"", b""]``); a
    policy-wide asset is its bare policy hex and never ``"lovelace"``.
    """
    alt, fields = _constr(value, what)
    if alt == _EXACT_ASSET and len(fields) == 1:
        policy, name = _list(fields[0], what, _PAIR)
        return asset_unit(_bytes(policy, what), _bytes(name, what)), False
    if alt == _POLICY_WIDE and len(fields) == 1:
        policy_hex = _hex(fields[0], what)
        if not policy_hex:
            msg = f"liqwid {what}: policy-wide asset with an empty policy"
            raise ValueError(msg)
        return policy_hex, True
    msg = f"liqwid {what}: expected an asset class"
    raise ValueError(msg)


def _market_state(fields: list) -> MarketStateDatum:
    what = "market state"
    return MarketStateDatum(
        cash=_int(fields[0], what),
        reserve=_int(fields[1], what),
        qtokens=_int(fields[2], what),
        principal=_int(fields[3], what),
        interest=_int(fields[4], what),
        index=_int(fields[5], what),
        rate=_rational(fields[6], what),
        last_interest_ms=_int(fields[7], what),
        last_batch_ms=_int(fields[8], what),
        qtoken_rate=_rational(fields[9], what),
        min_ada=_int(fields[10], what),
    )


def _withdraw_zero(value: object, what: str) -> tuple[str, str]:
    """The list36 withdraw-zero pair ``[Script[hash], Script[hash]]`` as hex."""
    hashes = []
    for credential in _list(value, what, _PAIR):
        alt, fields = _constr(credential, what)
        if alt != _SCRIPT_CREDENTIAL or len(fields) != 1:
            msg = f"liqwid {what}: expected a script credential"
            raise ValueError(msg)
        hashes.append(_hex(fields[0], what))
    return hashes[0], hashes[1]


def _market_params(fields: list) -> MarketParamsDatum:
    what = "market params"
    length = len(fields)
    capped = length >= _CAPPED_LAYOUT
    current = length >= _CURRENT_LAYOUT
    borrow_cap = _maybe(fields[_P_BORROW_CAP], what) if capped else None
    supply_cap = _maybe(fields[_P_SUPPLY_CAP], what) if capped else None
    return MarketParamsDatum(
        length=length,
        initial_qtoken_rate=_rational(fields[_P_INITIAL_QTOKEN_RATE], what),
        income_ratio=tuple(
            _int(part, what)
            for part in _list(fields[_P_INCOME_RATIO], what, _INCOME_RATIO_PARTS)
        ),
        legacy_loan_validator=_hex(fields[_P_LEGACY_LOAN_VALIDATOR], what),
        interest_period_ms=_int(fields[_P_INTEREST_PERIOD], what),
        rate_model=tuple(
            _rational(part, what)
            for part in _list(fields[_P_RATE_MODEL], what, _RATE_MODEL_PARTS)
        ),
        max_ltv=_rational(fields[_P_MAX_LTV], what),
        max_collaterals=_int(fields[_P_MAX_COLLATERALS], what),
        min_interest_rate=_rational(fields[_P_MIN_INTEREST_RATE], what),
        close_factor=_rational(fields[_P_CLOSE_FACTOR], what),
        liquidation_threshold=_rational(fields[_P_LIQUIDATION_THRESHOLD], what),
        action_validator=_hex(fields[_P_ACTION_VALIDATOR], what),
        batch_validator=_hex(fields[_P_BATCH_VALIDATOR], what),
        borrow_cap=None if borrow_cap is None else _rational(borrow_cap, what),
        supply_cap=None if supply_cap is None else _int(supply_cap, what),
        withdraw_zero=(
            _withdraw_zero(fields[_P_WITHDRAW_ZERO], what) if current else None
        ),
        loan_validator=_hex(fields[_P_LOAN_VALIDATOR], what) if current else None,
        raw=tuple(fields),
    )


def _collateral_params(fields: list) -> CollateralParamsDatum:
    what = "collateral params"
    collateral, policy_wide = _asset_class(fields[0], what)
    link = _maybe(fields[4], what)
    underlying = collateral_market_state = ""
    if link is not None:
        underlying_class, market_state = _list(link, what, _PAIR)
        underlying, _ = _asset_class(underlying_class, what)
        state_policy, state_name = _list(market_state, what, _PAIR)
        collateral_market_state = asset_unit(
            _bytes(state_policy, what),
            _bytes(state_name, what),
        )
    return CollateralParamsDatum(
        collateral=collateral,
        policy_wide=policy_wide,
        weight=_rational(fields[1], what),
        liquidation_discount=_rational(fields[2], what),
        treasury_split=_rational(fields[3], what),
        underlying=underlying,
        collateral_market_state=collateral_market_state,
    )


def _loan(fields: list) -> LoanDatum:
    what = "loan"
    return LoanDatum(
        owner=_hex(fields[0], what),
        principal=_int(fields[1], what),
        interest=_int(fields[2], what),
        min_interest=_int(fields[3], what),
        index=_int(fields[4], what),
    )


def _oracle_feed(fields: list) -> OracleFeedDatum:
    what = "oracle feed"
    asset, policy_wide = _asset_class(fields[1], what)
    return OracleFeedDatum(
        updater=_hex(fields[0], what),
        asset=asset,
        policy_wide=policy_wide,
        price=_rational(fields[2], what),
        ts_ms=_int(fields[3], what),
        validity_ms=_int(fields[4], what),
        caps=fields[_ORACLE_CAPS_FIELD] if len(fields) > _ORACLE_CAPS_FIELD else None,
    )


def _action(fields: list) -> ActionDatum:
    what = "action"
    deltas = [_int(part, what) for part in _list(fields[0], what, _ACTION_DELTAS)]
    return ActionDatum(
        supply_delta=deltas[0],
        qtoken_delta=deltas[1],
        principal_delta=deltas[2],
        interest_delta=deltas[3],
        min_interest_paid=deltas[4],
        nonce=fields[1],
    )


def decode_market_state(datum: str | bytes) -> MarketStateDatum:
    """Decode a MarketState record (list11). ``ValueError`` on any other shape."""
    return _decode(datum, MARKET_STATE_LENGTHS, "market state", _market_state)


def decode_market_params(datum: str | bytes) -> MarketParamsDatum:
    """Decode a MarketParams record of any layout in ``MARKET_PARAMS_LENGTHS``.

    The borrow cap is read only from p[32] of list34/list36. ``ValueError`` on any
    other length (a new layout) and on a parked ``Constr0[]`` output.
    """
    return _decode(datum, MARKET_PARAMS_LENGTHS, "market params", _market_params)


def decode_collateral_params(datum: str | bytes) -> CollateralParamsDatum:
    """Decode a CollateralParams record (list5), exact or script-class collateral."""
    return _decode(
        datum,
        COLLATERAL_PARAMS_LENGTHS,
        "collateral params",
        _collateral_params,
    )


def decode_loan(datum: str | bytes) -> LoanDatum:
    """Decode a loan record (list5). ``ValueError`` on a parked ``Constr0[]``."""
    return _decode(datum, LOAN_LENGTHS, "loan", _loan)


def decode_oracle_feed(datum: str | bytes) -> OracleFeedDatum:
    """Decode an oracle feed (list5 or list6), exact or policy-wide asset."""
    return _decode(datum, ORACLE_FEED_LENGTHS, "oracle feed", _oracle_feed)


def decode_action(datum: str | bytes) -> ActionDatum:
    """Decode an action record ``[[five signed deltas], nonce]``."""
    return _decode(datum, ACTION_LENGTHS, "action", _action)
