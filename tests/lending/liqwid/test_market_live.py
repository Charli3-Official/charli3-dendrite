"""Gated live Liqwid V2 checks over real dbsync (LIQWID_E2E=1).

Read-only: every query is a SELECT. Loads every live market and loan, compares the
loan count with the outputs at the loan validators, checks the bundled deployment
for drift against the live parameters, the published registry and the credentials
the identity tokens sit at, and runs every historical loan spend through the
successor matcher and the spend classifier. Skipped unless ``LIQWID_E2E=1`` (needs
reachable dbsync credentials in the ``DBSYNC_*`` environment or a ``.env``).
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import cbor2
import pytest
import requests
from dotenv import load_dotenv

from charli3_dendrite.backend.dbsync.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.liqwid.deployment import FIRST_SLOT
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import check_deployment
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from charli3_dendrite.lending.liqwid.lifecycle import LoanKey
from charli3_dendrite.lending.liqwid.lifecycle import LoanSpend
from charli3_dendrite.lending.liqwid.lifecycle import LoanTransition
from charli3_dendrite.lending.liqwid.lifecycle import classify_spend
from charli3_dendrite.lending.liqwid.lifecycle import loan_key
from charli3_dendrite.lending.liqwid.lifecycle import loan_spend
from charli3_dendrite.lending.liqwid.lifecycle import match_successors
from charli3_dendrite.lending.liqwid.market import LiqwidV2MarketSource
from charli3_dendrite.lending.liqwid.rates import PROTOCOL_NAME
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.units import plutus_fields

pytestmark = pytest.mark.skipif(
    os.environ.get("LIQWID_E2E") != "1",
    reason="requires live dbsync (LIQWID_E2E=1)",
)

REGISTRY_URL = "https://public.liqwid.finance/v6/registry.json"
LIVE_MARKETS = 35
LOAN_FIELDS = 5
POLICY_HEX_LEN = 56
SWEEP_CHUNK = 500
# The all-zero payment key hash: nobody holds its key, so a token sent there is
# retired, not moved to another validator.
BURN_CREDENTIAL = bytes(28)

# Outputs shaped by PoolSelector.select(), as the dbsync backend builds them.
_OUTPUTS_FROM = """
FROM (
    SELECT tx_out.*, address.address, address.payment_cred
    FROM tx_out
    JOIN address ON tx_out.address_id = address.id
    WHERE {where}
) AS txo
JOIN tx ON txo.tx_id = tx.id
JOIN block ON tx.block_id = block.id
JOIN datum ON txo.data_hash = datum.hash
"""

_LIVE_PARAMS_WHERE = """address.payment_cred = %(cred)b
      AND tx_out.consumed_by_tx_id IS NULL
      AND EXISTS (
          SELECT 1
          FROM ma_tx_out mto
          JOIN multi_asset ma ON ma.id = mto.ident
          WHERE mto.tx_out_id = tx_out.id AND ma.policy = ANY(%(policies)b)
      )"""

_SPENT_WHERE = """address.payment_cred = ANY(%(creds)b)
      AND tx_out.consumed_by_tx_id = ANY(%(ids)s::bigint[])"""

_CREATED_WHERE = """address.payment_cred = ANY(%(creds)b)
      AND tx_out.tx_id = ANY(%(ids)s::bigint[])"""

# Every unspent output at the loan validators, datum-less ones included.
_LOAN_OUTPUTS = """
SELECT ENCODE(tx.hash, 'hex') || '#' || tx_out.index::text AS out_ref,
       ENCODE(address.payment_cred, 'hex') AS credential,
       ENCODE(datum.bytes, 'hex') AS datum_cbor,
       ARRAY(
           SELECT ENCODE(ma.policy, 'hex')
           FROM ma_tx_out mto
           JOIN multi_asset ma ON ma.id = mto.ident
           WHERE mto.tx_out_id = tx_out.id
       ) AS policies
FROM tx_out
JOIN address ON tx_out.address_id = address.id
JOIN tx ON tx_out.tx_id = tx.id
LEFT JOIN datum ON tx_out.data_hash = datum.hash
WHERE address.payment_cred = ANY(%(creds)b)
  AND tx_out.consumed_by_tx_id IS NULL
"""

_IDENTITY_CREDENTIALS = """
SELECT DISTINCT ENCODE(address.payment_cred, 'hex') AS credential,
       ENCODE(ma.policy, 'hex') AS policy
FROM ma_tx_out mto
JOIN multi_asset ma ON ma.id = mto.ident
JOIN tx_out ON mto.tx_out_id = tx_out.id
JOIN address ON tx_out.address_id = address.id
WHERE ma.policy = ANY(%(policies)b)
  AND tx_out.consumed_by_tx_id IS NULL
  AND address.payment_cred IS NOT NULL
  AND address.payment_cred <> %(burn)b
"""

_SPENDING_TXS = """
SELECT DISTINCT tx_out.consumed_by_tx_id AS tx_id,
       ENCODE(spender.hash, 'hex') AS tx_hash,
       block.slot_no AS slot
FROM tx_out
JOIN address ON tx_out.address_id = address.id
JOIN tx AS spender ON tx_out.consumed_by_tx_id = spender.id
JOIN block ON spender.block_id = block.id
WHERE address.payment_cred = ANY(%(creds)b)
ORDER BY tx_id
"""

_WITHDRAWALS = """
SELECT withdrawal.tx_id, ENCODE(stake_address.script_hash, 'hex') AS script_hash
FROM withdrawal
JOIN stake_address ON withdrawal.addr_id = stake_address.id
WHERE withdrawal.tx_id = ANY(%(ids)s::bigint[])
  AND stake_address.script_hash IS NOT NULL
"""

_MINTS = """
SELECT ma_tx_mint.tx_id, ENCODE(ma.policy, 'hex') AS policy, ma_tx_mint.quantity
FROM ma_tx_mint
JOIN multi_asset ma ON ma_tx_mint.ident = ma.id
WHERE ma_tx_mint.tx_id = ANY(%(ids)s::bigint[])
"""


def _hex_bytes(values: Iterable[str]) -> list[bytes]:
    return [bytes.fromhex(value) for value in sorted(values)]


def _loan_credentials(deployment: LiqwidDeployment) -> list[bytes]:
    """Every loan validator the market source watches, every generation."""
    return _hex_bytes(
        {
            selector.payment_credential
            for selector in liqwid_selectors(deployment)
            if selector.kind is LiqwidKind.LOAN
        },
    )


def _field_count(datum_cbor: str | None) -> int | None:
    """Top-level field count of a datum: None without one, -1 when not a list."""
    if datum_cbor is None:
        return None
    try:
        return len(plutus_fields(cbor2.loads(bytes.fromhex(datum_cbor))))
    except (TypeError, ValueError, cbor2.CBORDecodeError):
        return -1


def _outputs(
    backend: Any,
    where: str,
    values: dict[str, Any],
    link: str = "tx_id",
) -> list[tuple[int, PoolStateInfo]]:
    """Outputs matching ``where``, each paired with its ``link`` transaction id."""
    query = (
        PoolSelector.select().rstrip()
        + f',\ntxo.{link} AS "link_tx_id"'
        + _OUTPUTS_FROM.format(where=where)
    )
    rows = backend.db_query(query, values)
    links = [row.pop("link_tx_id") for row in rows]
    return list(zip(links, PoolSelector.parse(rows)))


def _by_tx(rows: list[tuple[int, PoolStateInfo]]) -> dict[int, list[PoolStateInfo]]:
    grouped: dict[int, list[PoolStateInfo]] = defaultdict(list)
    for tx_id, info in rows:
        grouped[tx_id].append(info)
    return grouped


def _evidence(
    backend: Any,
    ids: list[int],
) -> tuple[dict[int, list[str]], dict[int, list[tuple[str, int]]]]:
    """Script withdrawals and mint rows of each transaction in ``ids``."""
    withdrawals: dict[int, list[str]] = defaultdict(list)
    for row in backend.db_query(_WITHDRAWALS, {"ids": ids}):
        withdrawals[row["tx_id"]].append(row["script_hash"])
    mints: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for row in backend.db_query(_MINTS, {"ids": ids}):
        mints[row["tx_id"]].append((row["policy"], int(row["quantity"])))
    return withdrawals, mints


def _classify_tx(
    spent_infos: list[PoolStateInfo],
    created_infos: list[PoolStateInfo],
    spend: LoanSpend,
    deployment: LiqwidDeployment,
) -> list[tuple[LoanKey, LoanTransition]]:
    """Every loan output one transaction spent, with its transition.

    Raises AssertionError when a link does not run from a spent loan to a created
    output of the same market, or when one created output continues two loans.
    """
    spent = [loan_key(info, deployment) for info in spent_infos]
    created = [loan_key(info, deployment) for info in created_infos]
    spent_keys = {key.out_ref: key for key in spent if key is not None}
    created_keys = {key.out_ref: key for key in created if key is not None}
    links = match_successors(list(spent_keys.values()), list(created_keys.values()))
    if not set(links) <= set(spent_keys):
        raise AssertionError(f"a successor for an output not spent: {links}")
    if not set(links.values()) <= set(created_keys):
        raise AssertionError(f"a successor that was not created: {links}")
    if len(set(links.values())) != len(links):
        raise AssertionError(f"one output continues two loans: {links}")
    for source, target in links.items():
        if spent_keys[source].market_id != created_keys[target].market_id:
            raise AssertionError(f"{source} continues in another market: {target}")
    outcomes = []
    for key in spent_keys.values():
        successor = created_keys.get(links.get(key.out_ref, ""))
        outcomes.append((key, classify_spend(key, successor, spend, deployment)))
    return outcomes


@pytest.fixture(scope="module")
def backend():
    load_dotenv()
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


@pytest.fixture(scope="module")
def deployment():
    return default_deployment()


@pytest.fixture(scope="module")
def snap(backend):
    return LiqwidV2MarketSource().load(backend)


def test_live_markets_and_loans(backend, deployment, snap):
    market_ids = {market.market_id for market in deployment.markets}
    assert len(snap.markets) == LIVE_MARKETS
    assert {market.market_id for market in snap.markets} == market_ids
    for market in snap.markets:
        assert market.protocol == PROTOCOL_NAME
        assert market.available_liquidity >= 0
        assert LendingMarket.from_dict(market.to_dict()).to_dict() == market.to_dict()

    rows = backend.db_query(_LOAN_OUTPUTS, {"creds": _loan_credentials(deployment)})
    loan_refs = set()
    non_loans = {}
    for row in rows:
        found = deployment.market_of_loan_validator(row["credential"])
        assert found is not None, row["out_ref"]
        holds_token = not found[0].loan_token_policies.isdisjoint(row["policies"])
        fields = _field_count(row["datum_cbor"])
        if holds_token and fields == LOAN_FIELDS:
            loan_refs.add(row["out_ref"])
            continue
        non_loans[row["out_ref"]] = (holds_token, fields)
        # A loan token outside a loan datum sits on a datum-less output or an
        # admin parking output (C0[]); any other shape is an unknown layout.
        if holds_token:
            assert fields in (None, 0), (row["out_ref"], fields)

    now_ms = time.time_ns() // 1_000_000
    positions = {position.position_id: position for position in snap.positions}
    assert len(positions) == len(snap.positions), "duplicate loan out-refs"
    assert len(snap.positions) == len(rows) - len(non_loans), sorted(non_loans.items())
    assert set(positions) == loan_refs
    for position in snap.positions:
        assert position.protocol == PROTOCOL_NAME
        assert position.origin is not None
        assert position.origin.identifier in market_ids
        assert position.current_debt(now_ms) >= position.principal
        restored = LendingPosition.from_dict(position.to_dict())
        assert restored.to_dict() == position.to_dict()


def test_live_deployment_has_no_drift(backend, deployment):
    params = [
        info
        for _, info in _outputs(
            backend,
            _LIVE_PARAMS_WHERE,
            {
                "cred": bytes.fromhex(deployment.param_credential),
                "policies": _hex_bytes(
                    market.market_params_policy for market in deployment.markets
                ),
            },
        )
    ]
    held = {
        unit[:POLICY_HEX_LEN]
        for info in params
        for unit in info.assets.root
        if unit != "lovelace"
    }
    assert {market.market_params_policy for market in deployment.markets} <= held
    # A parked C0[] output carries no parameters: it is not a MarketParams record.
    records = [info for info in params if _field_count(info.datum_cbor) != 0]

    response = requests.get(REGISTRY_URL, timeout=60)
    response.raise_for_status()

    identities = (
        {market.market_state_policy for market in deployment.markets}
        | deployment.loan_token_policies
        | {deployment.oracle_token_policy}
    )
    observed = [
        (row["credential"], row["policy"])
        for row in backend.db_query(
            _IDENTITY_CREDENTIALS,
            {"policies": _hex_bytes(identities), "burn": BURN_CREDENTIAL},
        )
    ]
    assert observed, "no live identity tokens"

    findings = check_deployment(
        records,
        registry=response.json(),
        observed_credentials=observed,
        deployment=deployment,
    )
    assert findings == [], "\n".join(
        f"{f.kind} {f.market} {f.component} {f.script_hash}: {f.detail}"
        for f in findings
    )


def test_every_historical_spend_classifies(backend, deployment):
    creds = _loan_credentials(deployment)
    names = {market.market_id: market.name for market in deployment.markets}
    spenders = backend.db_query(_SPENDING_TXS, {"creds": creds})
    assert spenders, "no historical loan spends"
    assert min(row["slot"] for row in spenders) >= FIRST_SLOT
    tally: Counter[tuple[str, str, str]] = Counter()
    failures: list[str] = []
    for start in range(0, len(spenders), SWEEP_CHUNK):
        chunk = spenders[start : start + SWEEP_CHUNK]
        ids = [row["tx_id"] for row in chunk]
        values = {"creds": creds, "ids": ids}
        spent = _by_tx(_outputs(backend, _SPENT_WHERE, values, "consumed_by_tx_id"))
        created = _by_tx(_outputs(backend, _CREATED_WHERE, values))
        withdrawals, mints = _evidence(backend, ids)
        for row in chunk:
            tx_id = row["tx_id"]
            try:
                outcomes = _classify_tx(
                    spent.get(tx_id, []),
                    created.get(tx_id, []),
                    loan_spend(withdrawals.get(tx_id, []), mints.get(tx_id, [])),
                    deployment,
                )
            except Exception as exc:  # every failure is collected and reported
                failures.append(f"{row['tx_hash']}: {exc!r}")
                continue
            for key, transition in outcomes:
                name = names.get(key.market_id, key.market_id)
                tally[(name, key.generation, transition.value)] += 1

    for (name, generation, transition), count in sorted(tally.items()):
        print(f"{name:>16} {generation:>7} {transition:>21} {count:>7}")
    assert sum(tally.values()) > 0, "no loan spend was classified"
    assert not failures, f"{len(failures)} failed spends:\n" + "\n".join(
        failures[:50],
    )
