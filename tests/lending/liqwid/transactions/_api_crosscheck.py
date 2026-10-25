"""Manual: compare these builders with Liqwid's public transaction API.

For a public address, the script asks Liqwid's GraphQL API
(``https://v2.api.liqwid.finance/graphql``, ``liqwid.transactions.*``) to build one
user transaction, rebuilds the same transaction with the snapshot ``from_backend``
of this package against the same chain state (the API's chosen action UTxO pinned,
the same wallet UTxOs as funding), and prints a diff of what both transactions
carry: the reference inputs by role, the redeemers by what they run on, the mints,
the protocol outputs (value and datum), the required signers and the validity
interval. Nothing is signed or submitted; the API returns an unsigned transaction
and the rebuild is never evaluated. The request names no ``wallet``, so the API adds
no wallet fee output.

Differences Liqwid's API is known to have are marked ``expected``:

- supply and redeem: the API requires the wallet's key as a signer; these
  builders add none;
- the upper validity bound: both default to about now + 900 s, capped at the
  market's batch deadline, from different clocks;
- redeem: the API pays ``floor(Q * s9) - 1`` for the qTokens it burns; these
  builders pay ``floor(Q * s9)`` and burn the fewest qTokens that cover the amount,
  so the burn, the action output and its datum may differ;
- the loan output: the API writes ``(161 + S) * K`` lovelace, these builders
  ``(160 + S) * K``, one byte's worth (``K`` lovelace) less;
- a datum whose bytes differ only in array encoding (definite here).

Every other difference is marked ``DIFFERS`` and the script exits with status 1.

Run (reads dbsync and Liqwid's API; never signs, never submits):

    DBSYNC_DSN="postgresql://..." PYTHONPATH=src:. python \\
        tests/lending/liqwid/transactions/_api_crosscheck.py supply \\
        --address addr1... --market Ada --amount 100000000

Sub-commands: ``supply``, ``redeem``, ``borrow``, ``increase``, ``repay`` and
``collateral``. Amounts are base units; a collateral is ``UNIT=QUANTITY`` with the
qToken unit (its 56-hex policy) or the collateral market's name. Liqwid's
``modifyBorrow`` takes the target total debt, measured from its current debt, the
payoff ``p0 + max(iS, m0)``: an increase of ``A`` asks for ``payoff + A``, a
partial repay of ``R`` for ``payoff - R``, a repay that reaches the payoff for
``0`` and a collateral change for ``payoff``. On a loan whose minimum interest
exceeds its settled interest the API's reading of that target is unverified. The API
caches a response for a few seconds; ``--bump N`` adds ``N`` base units to the
amount on both sides to get a fresh build. Without ``DBSYNC_DSN`` the five
``DBSYNC_*`` variables are read. Not run in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

import cbor2

from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import parse_action
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.lending.units import out_ref_key
from charli3_dendrite.lending.units import out_ref_text
from tests.lending.liqwid.transactions.replay import Built
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import protocol_outputs
from tests.lending.liqwid.transactions.replay import redeemers
from tests.lending.liqwid.transactions.replay import reference_inputs
from tests.lending.liqwid.transactions.replay import required_signers
from tests.lending.liqwid.transactions.replay import validity

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.liqwid.deployment import MarketComponents

API_URL = "https://v2.api.liqwid.finance/graphql"
POLICY_HEX_LEN = 56
# The lovelace one byte of an output costs (the ledger's coins per UTxO byte).
COINS_PER_BYTE = EvalContext(last_block_slot=0).protocol_param.coins_per_utxo_byte
_PLACEHOLDER_TX = "00" * 32

SAME = "same"
EXPECTED = "expected"
DIFFERS = "DIFFERS"

NOTE_SIGNER = (
    "supply and redeem: Liqwid's API requires the wallet's key; these builders add"
    " no signer"
)
NOTE_TTL = (
    "upper validity bound: both take about now + 900 s, capped at the batch"
    " deadline, from different clocks"
)
NOTE_REDEEM = (
    "redeem: Liqwid's API pays floor(Q * s9) - 1; these builders pay floor(Q * s9)"
    " and burn the fewest qTokens that cover the amount"
)
NOTE_LOAN_BYTE = (
    "loan lovelace: Liqwid's API writes (161 + S) * K, these builders (160 + S) * K"
)
NOTE_ENCODING = "datum encoding: the same value; these builders write definite arrays"

# The script roles of a market, by the MarketComponents field that lists them.
_SCRIPT_FIELDS = (
    ("actions", "action validator"),
    ("qtoken_policy", "qToken policy"),
    ("new_loan_validators", "loan validator"),
    ("legacy_loan_validators", "legacy loan validator"),
    ("new_borrow_policy", "loan-token policy"),
    ("borrow_policy", "legacy loan-token policy"),
    ("liquidation_policy", "liquidation policy"),
    ("borrow_staking", "borrow staking"),
    ("liquidation_staking", "liquidation staking"),
    ("market_state_validators", "MarketState validator"),
    ("batches", "batch validator"),
    ("batch_finals", "batch-final validator"),
)
_KIND_NAMES = {
    LiqwidKind.MARKET_PARAMS: "MarketParams",
    LiqwidKind.MARKET_STATE: "MarketState",
    LiqwidKind.LOAN: "loan",
}


# --- roles -------------------------------------------------------------------


def script_roles(deployment: LiqwidDeployment) -> dict[str, str]:
    """Script hash -> its role, as ``"<market> <role>"`` (joined when shared)."""
    roles: dict[str, set[str]] = {}
    for market in deployment.markets:
        for name, role in _SCRIPT_FIELDS:
            value = getattr(market, name)
            hashes = (value,) if isinstance(value, str) else value or ()
            for script_hash in hashes:
                roles.setdefault(script_hash, set()).add(f"{market.name} {role}")
    return {h: ", ".join(sorted(names)) for h, names in roles.items()}


def unit_label(unit: str, deployment: LiqwidDeployment) -> str:
    """A readable name of ``unit``.

    ``qAda`` for a qToken, ``Ada loan token 01`` for a loan token, the names of the
    markets that lend it (``NIGHT``) for an underlying, else the unit itself.
    """
    if unit == "lovelace":
        return unit
    policy, name = unit[:POLICY_HEX_LEN], unit[POLICY_HEX_LEN:]
    market = deployment.market_of_qtoken(policy)
    if market is not None and not name:
        return f"q{market.name}"
    for market in deployment.markets:
        if policy in market.loan_token_policies:
            return f"{market.name} loan token {name or '(empty)'}"
    lenders = sorted(m.name for m in deployment.markets if m.underlying == unit)
    return "/".join(lenders) if lenders else unit


def utxo_role(
    utxo: Utxo,
    deployment: LiqwidDeployment,
    scripts: Mapping[str, str],
) -> str:
    """What ``utxo`` is to a Liqwid transaction.

    A reference script (by the script it carries), a market record (MarketParams,
    MarketState, CollateralParams, loan), an oracle feed, a market's action UTxO,
    else ``"wallet"``.
    """
    if utxo.ref_script:
        script_hash = script_hash_of(utxo)
        return f"reference script: {scripts.get(script_hash, script_hash)}"
    info = utxo_to_info(utxo)
    record = parse_utxo(info, deployment)
    if record is not None and record.market_id:
        market = deployment.market(record.market_id)
        if record.kind is LiqwidKind.COLLATERAL_PARAMS:
            collateral = getattr(record.datum, "collateral", "")
            label = unit_label(collateral, deployment)
            return f"CollateralParams {market.name} / {label}"
        return f"{_KIND_NAMES.get(record.kind, record.kind.value)} {market.name}"
    feed = parse_feed(info, deployment)
    if feed is not None:
        return f"oracle feed {unit_label(feed.asset_unit, deployment)}"
    for market in deployment.markets:
        if parse_action(info, market) is not None:
            return f"action UTxO {market.name}"
    return "wallet"


# --- transaction views -------------------------------------------------------


@dataclass(frozen=True)
class OutputPart:
    """A protocol output as the diff compares it."""

    lovelace: int
    assets: tuple[tuple[str, int], ...]
    datum: str | None


@dataclass(frozen=True)
class TxView:
    """What a Liqwid transaction carries, keyed by role rather than position.

    ``references`` maps a role to its out-refs; ``spends`` a protocol input role
    to its out-refs; ``redeemers`` ``"<purpose> <what it runs on>"`` to the
    redeemer data (CBOR hex, as built); ``mints`` a unit to its quantity;
    ``outputs`` a protocol output role to its outputs.
    """

    references: dict[str, tuple[str, ...]]
    spends: dict[str, tuple[str, ...]]
    redeemers: dict[str, tuple[str, ...]]
    mints: dict[str, int]
    outputs: dict[str, tuple[OutputPart, ...]]
    signers: tuple[str, ...]
    validity: tuple[int | None, int | None]


def _body(cbor_hex: str) -> dict[int, Any]:
    return cbor2.loads(bytes.fromhex(cbor_hex))[0]


def _members(value: Any) -> list[Any]:  # noqa: ANN401
    if value is None:
        return []
    if isinstance(value, cbor2.CBORTag):
        value = value.value
    return list(value)


def spent_inputs(cbor_hex: str) -> list[str]:
    """The out-refs a transaction spends, in ledger order (the spend index order)."""
    refs = [(bytes(h).hex(), int(i)) for h, i in _members(_body(cbor_hex).get(0))]
    return [out_ref_text(ref) for ref in sorted(refs)]


def tx_out_refs(cbor_hex: str) -> set[str]:
    """Every out-ref a transaction spends or references."""
    built = Built(cbor_hex)
    return set(spent_inputs(cbor_hex)) | {
        out_ref_text(ref) for ref in reference_inputs(built)
    }


def _mint_policies(cbor_hex: str) -> list[str]:
    return sorted(bytes(policy).hex() for policy in _body(cbor_hex).get(9) or {})


def _withdrawal_credentials(cbor_hex: str) -> list[str]:
    addresses = sorted(bytes(a) for a in _body(cbor_hex).get(5) or {})
    return [a[1:].hex() for a in addresses]


def _output_utxo(view: Any, index: int) -> Utxo:  # noqa: ANN401
    """A ``Utxo`` of a built output, at a placeholder out-ref, for its role."""
    return Utxo(
        address=view.address,
        lovelace=view.lovelace,
        assets=sorted(
            (unit[:POLICY_HEX_LEN], unit[POLICY_HEX_LEN:], qty)
            for unit, qty in view.assets
        ),
        datum=view.datum,
        out_ref=(_PLACEHOLDER_TX, index),
        datum_hash=view.datum_hash,
    )


def _role_of(
    ref: str,
    lookup: Mapping[str, Utxo],
    deployment: LiqwidDeployment,
    scripts: Mapping[str, str],
) -> str:
    utxo = lookup.get(ref)
    return "unresolved" if utxo is None else utxo_role(utxo, deployment, scripts)


def _grouped(pairs: Iterable[tuple[str, Any]]) -> dict[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for key, value in pairs:
        grouped.setdefault(key, []).append(value)
    return {key: tuple(sorted(values)) for key, values in grouped.items()}


def tx_view(
    cbor_hex: str,
    lookup: Mapping[str, Utxo],
    *,
    deployment: LiqwidDeployment | None = None,
) -> TxView:
    """The :class:`TxView` of the transaction ``cbor_hex``.

    ``lookup`` maps ``"<tx hash>#<index>"`` to the UTxO at every out-ref the
    transaction spends or references; an out-ref it lacks reads as the role
    ``"unresolved"``.
    """
    active = deployment or default_deployment()
    scripts = script_roles(active)
    built = Built(cbor_hex)
    inputs = spent_inputs(cbor_hex)
    policies = _mint_policies(cbor_hex)
    credentials = _withdrawal_credentials(cbor_hex)

    def role(ref: str) -> str:
        return _role_of(ref, lookup, active, scripts)

    def target(purpose: str, index: int) -> str:
        if purpose == "spend" and index < len(inputs):
            return role(inputs[index])
        if purpose == "mint" and index < len(policies):
            return scripts.get(policies[index], policies[index])
        if purpose == "reward" and index < len(credentials):
            return scripts.get(credentials[index], credentials[index])
        return f"#{index}"

    outputs = []
    for index, view in enumerate(
        protocol_outputs(built, set(active.payment_credentials))
    ):
        utxo = _output_utxo(view, index)
        part = OutputPart(view.lovelace, tuple(sorted(view.assets)), view.datum)
        outputs.append((utxo_role(utxo, active, scripts), part))
    return TxView(
        references=_grouped(
            (role(out_ref_text(ref)), out_ref_text(ref))
            for ref in reference_inputs(built)
        ),
        spends=_grouped((r, ref) for ref in inputs if (r := role(ref)) != "wallet"),
        redeemers=_grouped(
            (f"{purpose} {target(purpose, index)}", data)
            for purpose, index, data in redeemers(built)
        ),
        mints={unit: qty for unit, qty in mints(built)},
        outputs=_grouped(outputs),
        signers=tuple(required_signers(built)),
        validity=validity(built),
    )


# --- diff --------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One compared item: its section, key, both values, status and note."""

    section: str
    key: str
    api: Any
    ours: Any
    status: str
    note: str = ""


@dataclass
class _Rows:
    flow: Flow
    deployment: LiqwidDeployment
    rows: list[Row] = field(default_factory=list)

    def add(
        self,
        section: str,
        key: str,
        api: Any,  # noqa: ANN401
        ours: Any,  # noqa: ANN401
        note: str | None = None,
    ) -> None:
        if api == ours:
            self.rows.append(Row(section, key, api, ours, SAME))
        elif note:
            self.rows.append(Row(section, key, api, ours, EXPECTED, note))
        else:
            self.rows.append(Row(section, key, api, ours, DIFFERS))


def _same_value(api_datum: str | None, our_datum: str | None) -> bool:
    if api_datum is None or our_datum is None:
        return False
    return cbor2.loads(bytes.fromhex(api_datum)) == cbor2.loads(
        bytes.fromhex(our_datum),
    )


def _compare_outputs(
    rows: _Rows,
    role: str,
    api: tuple[OutputPart, ...],
    ours: tuple[OutputPart, ...],
) -> None:
    if len(api) != len(ours):
        rows.add("outputs", role, api, ours)
        return
    redeem = rows.flow is Flow.REDEEM and role.startswith("action UTxO")
    loan = role.startswith("loan ")
    for n, (a, o) in enumerate(zip(api, ours)):
        key = role if len(api) == 1 else f"{role} [{n}]"
        lovelace_note = None
        if redeem:
            lovelace_note = NOTE_REDEEM
        elif loan and a.lovelace - o.lovelace == COINS_PER_BYTE:
            lovelace_note = NOTE_LOAN_BYTE
        rows.add("outputs", f"{key} lovelace", a.lovelace, o.lovelace, lovelace_note)
        rows.add(
            "outputs",
            f"{key} assets",
            a.assets,
            o.assets,
            NOTE_REDEEM if redeem else None,
        )
        datum_note = None
        if _same_value(a.datum, o.datum):
            datum_note = NOTE_ENCODING
        elif redeem:
            datum_note = NOTE_REDEEM
        rows.add("outputs", f"{key} datum", a.datum, o.datum, datum_note)


def compare(
    api: TxView,
    ours: TxView,
    *,
    flow: Flow,
    deployment: LiqwidDeployment | None = None,
) -> list[Row]:
    """Compare Liqwid's API build ``api`` with ``ours`` for ``flow``, item by item.

    Every key either side carries gets a row; a row that differs is ``expected``
    when one of the known differences of Liqwid's API explains it.
    """
    deployment = deployment or default_deployment()
    rows = _Rows(flow, deployment)
    for role in sorted(api.references.keys() | ours.references.keys()):
        rows.add(
            "reference inputs",
            role,
            api.references.get(role),
            ours.references.get(role),
        )
    for role in sorted(api.spends.keys() | ours.spends.keys()):
        rows.add("spent inputs", role, api.spends.get(role), ours.spends.get(role))
    for key in sorted(api.redeemers.keys() | ours.redeemers.keys()):
        rows.add("redeemers", key, api.redeemers.get(key), ours.redeemers.get(key))
    for unit in sorted(api.mints.keys() | ours.mints.keys()):
        label = unit_label(unit, deployment)
        burn = flow is Flow.REDEEM and (
            deployment.market_of_qtoken(unit[:POLICY_HEX_LEN]) is not None
        )
        rows.add(
            "mints",
            label if label == unit else f"{label} ({unit})",
            api.mints.get(unit),
            ours.mints.get(unit),
            NOTE_REDEEM if burn else None,
        )
    for role in sorted(api.outputs.keys() | ours.outputs.keys()):
        _compare_outputs(
            rows,
            role,
            api.outputs.get(role, ()),
            ours.outputs.get(role, ()),
        )
    signer_note = None
    if (
        flow in (Flow.SUPPLY, Flow.REDEEM)
        and not ours.signers
        and len(api.signers) == 1
    ):
        signer_note = NOTE_SIGNER
    rows.add("signers", "required signers", api.signers, ours.signers, signer_note)
    rows.add("validity", "lower bound (slot)", api.validity[0], ours.validity[0])
    rows.add(
        "validity",
        "upper bound (slot)",
        api.validity[1],
        ours.validity[1],
        NOTE_TTL if None not in (api.validity[1], ours.validity[1]) else None,
    )
    return rows.rows


def differences(rows: Iterable[Row]) -> list[Row]:
    """The rows that are not the same on both sides."""
    return [row for row in rows if row.status != SAME]


def unexpected(rows: Iterable[Row]) -> list[Row]:
    """The rows no known difference of Liqwid's API explains."""
    return [row for row in rows if row.status == DIFFERS]


def _shown(value: Any) -> str:  # noqa: ANN401
    if value is None:
        return "(absent)"
    if isinstance(value, tuple) and all(isinstance(v, str) for v in value):
        return ", ".join(value) or "(none)"
    return str(value)


def render(rows: Iterable[Row], *, title: str = "") -> str:
    """The diff as text: one line per item, both sides under any difference."""
    rows = list(rows)
    lines = [title] if title else []
    section = None
    for row in rows:
        if row.section != section:
            section = row.section
            lines.append(f"== {section}")
        if row.status == SAME:
            lines.append(f"  {SAME:<9} {row.key}: {_shown(row.api)}")
            continue
        lines.append(f"  {row.status:<9} {row.key}")
        lines.append(f"            api   {_shown(row.api)}")
        lines.append(f"            ours  {_shown(row.ours)}")
        if row.note:
            lines.append(f"            note  {row.note}")
    differing = differences(rows)
    bad = unexpected(rows)
    lines.append(
        f"{len(rows)} items: {len(rows) - len(differing)} same, "
        f"{len(differing) - len(bad)} expected, {len(bad)} unexpected",
    )
    return "\n".join(lines)


# --- Liqwid's API ------------------------------------------------------------


_BUILDERS = {
    Flow.SUPPLY: ("supply", "SupplyTransactionInput"),
    Flow.REDEEM: ("withdraw", "WithdrawTransactionInput"),
    Flow.OPEN: ("borrow", "BorrowTransactionInput"),
    Flow.INCREASE: ("modifyBorrow", "ModifyBorrowTransactionInput"),
    Flow.REPAY: ("modifyBorrow", "ModifyBorrowTransactionInput"),
    Flow.CLOSE: ("modifyBorrow", "ModifyBorrowTransactionInput"),
    Flow.COLLATERAL: ("modifyBorrow", "ModifyBorrowTransactionInput"),
}


def build_query(flow: Flow) -> tuple[str, str]:
    """The API builder field of ``flow`` and the GraphQL query that calls it."""
    name, input_type = _BUILDERS[flow]
    query = (
        f"query Build($input: {input_type}!) "
        f"{{ liqwid {{ transactions {{ {name}(input: $input) {{ cbor }} }} }} }}"
    )
    return name, query


def cip30_utxo(utxo: Utxo) -> str:
    """``utxo`` as a CIP-30 ``TransactionUnspentOutput``: ``[input, output]`` hex."""
    from charli3_dendrite.lending.transactions.utxos import to_pycardano_utxo

    pyc = to_pycardano_utxo(utxo)
    return (b"\x82" + pyc.input.to_cbor() + pyc.output.to_cbor()).hex()


def api_loan_id(loan_utxo: str) -> str:
    """The API's loan id of ``"<tx hash>#<index>"``: ``"<tx hash>-<index>"``."""
    tx_hash, index = out_ref_key(loan_utxo)
    return f"{tx_hash}-{index}"


def api_collaterals(
    borrow_market: str,
    collateral: Mapping[str, int],
) -> list[dict[str, Any]]:
    """The API's collateral list of a target ``{qToken unit: quantity}``.

    Each id is ``"<borrow market name>.<collateral qToken policy>"``; a unit with an
    asset name also carries ``tokenName``.
    """
    entries = []
    for unit, qty in sorted(collateral.items()):
        if qty <= 0:
            continue
        entry: dict[str, Any] = {
            "id": f"{borrow_market}.{unit[:POLICY_HEX_LEN]}",
            "amount": qty,
        }
        if unit[POLICY_HEX_LEN:]:
            entry["tokenName"] = unit[POLICY_HEX_LEN:]
        entries.append(entry)
    return entries


def modify_borrow_target(flow: Flow, *, amount: int, current_debt: int) -> int:
    """The total debt ``modifyBorrow`` is asked for.

    ``amount`` is the borrow more ``A`` (``INCREASE``) or the repay ``R``
    (``REPAY``); ``current_debt`` is the loan's debt as the API reports it, the
    payoff. A close asks for 0 and a collateral change for ``current_debt``.

    Raises:
        ValueError: ``flow`` is not a loan modification.
    """
    if flow is Flow.INCREASE:
        return current_debt + amount
    if flow is Flow.REPAY:
        return current_debt - amount
    if flow is Flow.CLOSE:
        return 0
    if flow is Flow.COLLATERAL:
        return current_debt
    msg = f"{flow.value} is not a loan modification"
    raise ValueError(msg)


def request_build(
    flow: Flow,
    variables: Mapping[str, Any],
    *,
    url: str = API_URL,
    timeout: float = 120.0,
) -> str:
    """Ask Liqwid's API to build ``flow`` from ``variables``; the unsigned CBOR hex.

    Raises:
        RuntimeError: The API answers with errors (its builder's refusal or a
            failed evaluation) or without a transaction.
    """
    name, query = build_query(flow)
    body = json.dumps({"query": query, "variables": {"input": dict(variables)}})
    request = urllib.request.Request(  # noqa: S310 - a fixed https endpoint
        url,
        data=body.encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        answer = json.load(response)
    if answer.get("errors"):
        raise RuntimeError(json.dumps(answer["errors"])[:2000])
    try:
        return answer["data"]["liqwid"]["transactions"][name]["cbor"]
    except (KeyError, TypeError):
        msg = f"the API returned no {name} transaction: {json.dumps(answer)[:2000]}"
        raise RuntimeError(msg) from None


# --- live run ----------------------------------------------------------------


def dbsync_backend() -> AbstractBackend:
    """A dbsync backend, its ``DBSYNC_*`` taken from ``DBSYNC_DSN`` when set."""
    from psycopg.conninfo import conninfo_to_dict

    from tests.lending.liqwid.transactions.replay import dbsync_connect_kwargs

    kwargs = dbsync_connect_kwargs()
    if "conninfo" in kwargs:
        parts = conninfo_to_dict(kwargs["conninfo"])
        names = {
            "host": "DBSYNC_HOST",
            "port": "DBSYNC_PORT",
            "dbname": "DBSYNC_DB_NAME",
            "user": "DBSYNC_USER",
            "password": "DBSYNC_PASS",
        }
        for key, env in names.items():
            if parts.get(key) is not None:
                os.environ[env] = str(parts[key])
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def parse_collateral(
    items: Iterable[str],
    deployment: LiqwidDeployment,
) -> dict[str, int]:
    """``UNIT=QUANTITY`` arguments as ``{qToken unit: quantity}``.

    ``UNIT`` is a qToken unit (56-hex policy, optionally with a hex asset name) or
    the name of the market whose qToken it is.

    Raises:
        ValueError: An item is not ``UNIT=QUANTITY``.
        KeyError: No market has that name.
    """
    target: dict[str, int] = {}
    for item in items:
        unit, sep, qty = item.partition("=")
        if not sep:
            msg = f"collateral {item!r} is not UNIT=QUANTITY"
            raise ValueError(msg)
        if len(unit) < POLICY_HEX_LEN or not all(c in "0123456789abcdef" for c in unit):
            unit = deployment.market_by_name(unit).qtoken_policy
        target[unit] = target.get(unit, 0) + int(qty)
    return target


@dataclass(frozen=True)
class _Loan:
    """A loan's market, payoff and collateral, read live."""

    market: MarketComponents
    payoff: int
    collateral: dict[str, int]


def _resolve_loan(backend: AbstractBackend, loan_utxo: str) -> _Loan:
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
    from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market
    from charli3_dendrite.lending.liqwid.transactions.terms import close_payment

    loan = resolve_loan(backend, loan_utxo)
    s5 = resolve_market(backend, loan.market).state_datum.index
    return _Loan(
        market=loan.market,
        payoff=close_payment(loan.datum, s5),
        collateral=loan.collateral,
    )


def _api_variables(
    flow: Flow,
    args: argparse.Namespace,
    *,
    utxos: list[str],
    collateral: dict[str, int],
    loan: _Loan | None,
) -> dict[str, Any]:
    variables: dict[str, Any] = {"address": args.address, "utxos": utxos}
    if flow in (Flow.SUPPLY, Flow.REDEEM, Flow.OPEN):
        variables.update(marketId=args.market, amount=args.amount)
    if flow is Flow.OPEN:
        variables["collaterals"] = api_collaterals(args.market, collateral)
    if loan is not None:
        target = collateral or loan.collateral
        variables.update(
            txId=api_loan_id(args.loan),
            amount=modify_borrow_target(
                flow,
                amount=args.amount or 0,
                current_debt=loan.payoff,
            ),
            collaterals=api_collaterals(loan.market.name, target),
        )
    return variables


def _our_build(
    backend: AbstractBackend,
    flow: Flow,
    args: argparse.Namespace,
    *,
    funding: list[Utxo],
    collateral: dict[str, int],
    pin: str | None,
    loan: _Loan | None,
) -> Built:
    """Resolve and build ``flow`` with this package, pinned to ``pin``."""
    from pycardano import TransactionBuilder

    from charli3_dendrite.lending.liqwid.transactions import borrow
    from charli3_dendrite.lending.liqwid.transactions import borrow_more
    from charli3_dendrite.lending.liqwid.transactions import collateral as coll
    from charli3_dendrite.lending.liqwid.transactions import repay
    from charli3_dendrite.lending.liqwid.transactions import supply
    from charli3_dendrite.lending.transactions.infra import assemble_unsigned
    from charli3_dendrite.lending.transactions.infra import current_slot

    common = {"owner_address": args.address, "funding": funding}
    snapshot: Any
    build: Callable[..., None]
    if flow is Flow.SUPPLY:
        snapshot = supply.SupplySnapshot.from_backend(
            backend,
            market=args.market,
            amount=args.amount,
            action_utxo=pin,
            **common,
        )
        build = supply.build_supply
    elif flow is Flow.REDEEM:
        snapshot = supply.RedeemSnapshot.from_backend(
            backend,
            market=args.market,
            amount=args.amount,
            action_utxo=pin,
            **common,
        )
        build = supply.build_redeem
    elif flow is Flow.OPEN:
        snapshot = borrow.BorrowSnapshot.from_backend(
            backend,
            market=args.market,
            amount=args.amount,
            collateral=collateral,
            action_utxo=pin,
            **common,
        )
        build = borrow.build_borrow
    elif flow is Flow.INCREASE:
        snapshot = borrow_more.IncreaseSnapshot.from_backend(
            backend,
            loan_utxo=args.loan,
            amount=args.amount,
            collateral=collateral or None,
            action_utxo=pin,
            **common,
        )
        build = borrow_more.build_increase
    elif flow in (Flow.REPAY, Flow.CLOSE):
        assert loan is not None
        snapshot = repay.RepaySnapshot.from_backend(
            backend,
            loan_utxo=args.loan,
            repay=loan.payoff if flow is Flow.CLOSE else args.amount,
            collateral=collateral or None,
            action_utxo=pin,
            **common,
        )
        build = repay.build_repay
    else:
        snapshot = coll.CollateralSnapshot.from_backend(
            backend,
            loan_utxo=args.loan,
            collateral=collateral,
            **common,
        )
        build = coll.build_collateral
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=current_slot(backend)))
    build(tx_builder, snapshot=snapshot)
    return Built(assemble_unsigned(tx_builder))


def _lookup(backend: AbstractBackend, refs: Iterable[str]) -> dict[str, Utxo]:
    from charli3_dendrite.lending.transactions.resolve import resolve_utxo_by_outref

    found = {}
    for ref in sorted(refs):
        tx_hash, index = out_ref_key(ref)
        try:
            found[ref] = resolve_utxo_by_outref(
                backend,
                tx_hash,
                index,
                allow_spent=True,
            )
        except ValueError:
            continue
    return found


def _flow_of(args: argparse.Namespace, loan: _Loan | None) -> Flow:
    if args.command == "repay" and loan is not None:
        if args.close or args.amount >= loan.payoff:
            return Flow.CLOSE
        return Flow.REPAY
    return _COMMANDS[args.command]


_COMMANDS = {
    "supply": Flow.SUPPLY,
    "redeem": Flow.REDEEM,
    "borrow": Flow.OPEN,
    "increase": Flow.INCREASE,
    "repay": Flow.REPAY,
    "collateral": Flow.COLLATERAL,
}


def run(args: argparse.Namespace) -> int:
    """Build ``args``' flow both ways, print the diff; 1 on an unexpected one."""
    from charli3_dendrite.lending.transactions.resolve import resolve_funding

    deployment = default_deployment()
    backend = dbsync_backend()
    collateral = parse_collateral(args.collateral or (), deployment)
    if args.amount is not None:
        args.amount += args.bump
    elif args.bump and collateral:
        first = sorted(collateral)[0]
        collateral[first] += args.bump
    loan = _resolve_loan(backend, args.loan) if getattr(args, "loan", None) else None
    flow = _flow_of(args, loan)
    funding = resolve_funding(backend, args.address)
    variables = _api_variables(
        flow,
        args,
        utxos=[cip30_utxo(u) for u in funding],
        collateral=collateral,
        loan=loan,
    )
    api_cbor = request_build(flow, variables, url=args.endpoint)
    lookup = _lookup(backend, tx_out_refs(api_cbor))
    api = tx_view(api_cbor, lookup, deployment=deployment)
    pins = [
        ref
        for role, refs in api.spends.items()
        if role.startswith("action UTxO")
        for ref in refs
    ]
    if len(pins) > 1:
        msg = f"the API build spends several action UTxOs: {pins}"
        raise RuntimeError(msg)
    built = _our_build(
        backend,
        flow,
        args,
        funding=funding,
        collateral=collateral,
        pin=pins[0] if pins else None,
        loan=loan,
    )
    lookup.update(_lookup(backend, tx_out_refs(built.cbor) - lookup.keys()))
    ours = tx_view(built.cbor, lookup, deployment=deployment)
    rows = compare(api, ours, flow=flow, deployment=deployment)
    title = f"{flow.value}: Liqwid API ({len(api_cbor) // 2} bytes) vs these builders"
    print(render(rows, title=title))  # noqa: T201
    if args.show_cbor:
        print(f"api  {api_cbor}\nours {built.cbor}")  # noqa: T201
    return 1 if unexpected(rows) else 0


def parser() -> argparse.ArgumentParser:
    """The command line: one sub-command per flow."""
    top = argparse.ArgumentParser(
        prog="_api_crosscheck.py",
        description=(
            "Build one Liqwid V2 transaction with Liqwid's API and with these"
            " builders from the same chain state, and diff them. Never signs or"
            " submits."
        ),
    )
    top.add_argument("--endpoint", default=API_URL, help="the GraphQL endpoint")
    top.add_argument(
        "--bump",
        type=int,
        default=0,
        help="base units added to the amount on both sides (bypasses the API cache)",
    )
    top.add_argument("--show-cbor", action="store_true", help="print both CBORs")
    commands = top.add_subparsers(dest="command", required=True)

    def command(name: str, text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=text, description=text)
        sub.add_argument("--address", required=True, help="the wallet's address")
        return sub

    def collateral_arg(sub: argparse.ArgumentParser, *, required: bool) -> None:
        sub.add_argument(
            "--collateral",
            action="append",
            required=required,
            metavar="UNIT=QUANTITY",
            help="a target collateral: qToken unit or market name, qToken units",
        )

    for name, text in (
        ("supply", "supply AMOUNT of the market's underlying"),
        ("redeem", "redeem AMOUNT of the market's underlying"),
    ):
        sub = command(name, text)
        sub.add_argument("--market", required=True, help="market name, e.g. Ada")
        sub.add_argument("--amount", type=int, required=True)
    sub = command("borrow", "open a loan of AMOUNT against the collateral")
    sub.add_argument("--market", required=True, help="the borrow market's name")
    sub.add_argument("--amount", type=int, required=True)
    collateral_arg(sub, required=True)
    sub = command("increase", "borrow AMOUNT more on a loan")
    sub.add_argument("--loan", required=True, help="the loan's tx_hash#index")
    sub.add_argument("--amount", type=int, required=True)
    collateral_arg(sub, required=False)
    sub = command("repay", "repay AMOUNT of a loan (at least the payoff closes it)")
    sub.add_argument("--loan", required=True, help="the loan's tx_hash#index")
    group = sub.add_mutually_exclusive_group(required=True)
    group.add_argument("--amount", type=int)
    group.add_argument("--close", action="store_true", help="repay the payoff")
    collateral_arg(sub, required=False)
    sub = command("collateral", "change a loan's collateral to the target")
    sub.add_argument("--loan", required=True, help="the loan's tx_hash#index")
    collateral_arg(sub, required=True)
    sub.set_defaults(amount=None)
    return top


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run the cross-check."""
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
