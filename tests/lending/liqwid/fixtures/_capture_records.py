"""Dev-only: capture Liqwid V2 mainnet records and Liqwid API values at one chain tip.

Writes two files next to this script:

``records.json.gz``: gzipped JSON (``mtime=0``, so re-capturing identical data is
byte-identical; the live loan book alone is over the repository's 500 KB file limit
as indented JSON). Every UTxO entry is ``{out_ref, address, datum_cbor, assets, slot,
block_index, block_time, spent_slot, live, market_id, generation, label}``:
``assets`` is the charli3 ``Assets`` shape, ``block_time`` POSIX seconds,
``spent_slot`` the slot of the consuming transaction (null while unspent), ``live``
whether the output exists at the snapshot slot, ``market_id`` the MarketState NFT
policy of the entry's market, ``generation`` the loan-validator generation and
``label`` None for a genuine record or the reason it is not one. Keys:

- ``market_params``: one output per datum layout, a parked ``C0[]`` output, one
  ADA-only output at the parameter address, every live record and every record alive
  at a captured historical MarketState;
- ``collateral_params``: every live record, the genesis Ada record, one record under a
  retired policy, the script-class (``C0[policy]``) record and every record alive at a
  captured historical MarketState;
- ``market_state``: the first state at each launch validator, the ``list0`` and
  ``list27`` outputs that once held the Ada MarketState NFT, every live state;
- ``loan``: legacy indefinite, new definite and indefinite, 0, 1 and 5 collaterals, a
  ``30``/``31`` loan-token name, a parking ``C0[]`` output, a datum-less output, a
  script-class holding, every live output at a loan validator;
- ``oracle``: the first feed at each oracle validator, pegs in both layouts, a
  policy-wide (``Constr0``) feed, the token-less decoys, every live feed;
- ``spends``: one consuming transaction per lifecycle rule (``SPEND_RULES``);
- ``max_borrows``: the borrows that met a borrow cap.

``api_snapshot.json``: ``{slot, loans: {out_ref: {debt, interest, healthFactor,
LTV}}, markets: {market_id: {borrowAPR, supplyAPR, utilization, exchangeRate,
liquidity, borrowCap}}}`` from the public GraphQL API; ``supplyAPR`` is the API's
``supplyAPY`` field and ``borrowCap`` its ``parameters.borrowCap``.

Run: PYTHONPATH=src:. python tests/lending/liqwid/fixtures/_capture_records.py
Requires dbsync env (DBSYNC_*) and network access. Not run in CI.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

import cbor2
import psycopg
import requests
from dotenv import load_dotenv

from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.units import plutus_fields
from charli3_dendrite.lending.units import script_credential
from tests.lending._capture_dbsync import record_entry
from tests.lending._capture_dbsync import record_sql

load_dotenv()

HERE = Path(__file__).parent
API_URL = "https://v2.api.liqwid.finance/graphql"

# One MarketParams output per datum layout: list length -> (tx hash, output index).
MARKET_PARAMS_LAYOUTS = {
    27: ("36d4bfe57e1ebd90e0e586f228b84903403e72c9d6d54b88994f66a0372f18ee", 1),
    30: ("8eb900a137226047109f92f1bd1c73d18d3ce1b55ba621345ffc9acb727d9f56", 0),
    32: ("e51f0164dba6c22842e7ae8f539582c2e5b425ea6ac2e824beaa7bfb29665fba", 3),
    34: ("6e8dab3ab0d52a00f848ec209ec6fd37504afa4076d1bbfc5abccf85d061d9da", 10),
    36: ("52474aff233de3dc5a75b80de64ceb4ae63aabb99cd7bd408c51349979ed9a2b", 0),
}
# A MarketParams NFT parked on an empty constructor.
PARKED_MARKET_PARAMS = (
    "88d368eaf1347e4b8643e17983dff1826252b38e0bc740f7c65bd2d7c3651f62",
    0,
)
# MarketState validators of the launch period (absent from registry v6), by market.
LAUNCH_STATE_VALIDATORS = {
    "Ada": "0fd1b854729d2e29290d47fe2173319aae3c4c885a955dc203f72759",
    "DJED": "63d65418f841e9901d5ee634cc07377cca2be7fb2b797e3f0e0d8334",
    "SHEN": "89ea1358d8147feb5ef1875fbad449f65719ced6102ce1600bf093a7",
    "IUSD": "9885c2bc619cb1e09d63c59eebef3c27e7b796cdca3ff89fdbfb4fa1",
}
# A CollateralParams NFT policy that no market uses any more.
DEAD_COLLATERAL_PARAMS_PREFIX = "90316fe4"
# ``classify_spend`` rule (1-8) each captured spend exercises; None = the
# state-match pass of ``match_successors``.
SPEND_RULES = {
    "partial_liquidation": 1,
    "new_modification": 2,
    "full_repay": 2,
    "legacy_liquidation": 3,
    "legacy_repay": 4,
    "legacy_flip": 5,
    "admin_migration": 6,
    "admin_reown": 6,
    "parking": 7,
    "same_generation_rewrite": 8,
    "multi_loan": None,
}
# Spends named by transaction hash (or a unique prefix): rule -> hash.
NAMED_SPENDS = {
    "partial_liquidation": (
        "17a4aabef5ee2bc954b6612496cdef0a7c1393530f660a1e0aa872797abd993f"
    ),
    "new_modification": (
        "852f9f7512731e4f43c721e4e4deb9c38b11f5eb648422ae98d4ebc00f12f921"
    ),
    "full_repay": "b114f920abad087eff25c116ec457f34b742659a9b9fe3389bd7a6bf1fc04a99",
    "admin_migration": (
        "79e2bda3acd86e9188c5a008fb825437e9d91a04d871b38961f05d907cc00902"
    ),
    "admin_reown": "c3115839d26da42d8f38b002136fd490bb17f137978923b2a2e93b2ba2151e32",
    "same_generation_rewrite": "3a50a14b",
}
# Transition of a spent loan per rule: (with a successor, without one).
RULE_TRANSITIONS = {
    "partial_liquidation": ("partially_liquidated", "liquidated"),
    "new_modification": ("modified", "repaid"),
    "full_repay": ("modified", "repaid"),
    "legacy_liquidation": ("partially_liquidated", "liquidated"),
    "legacy_repay": ("modified", "repaid"),
    "legacy_flip": ("modified", "repaid"),
    "admin_migration": ("migrated", "parked"),
    "admin_reown": ("migrated", "parked"),
    "parking": ("migrated", "parked"),
    "same_generation_rewrite": ("migrated", "parked"),
    "multi_loan": ("migrated", "parked"),
}
# Borrows that met a borrow cap: market name -> (first slot, last slot, the net
# principal range of one borrow, number of such borrows in the window).
CAPPED_BORROW_WINDOWS = {
    "USDM": (136_865_709, 136_952_109, (49_494_000_000, 49_495_000_000), 4),
    "STRIKE-ADA": (196_049_709, 196_136_109, (1_336_847_000, 1_336_848_000), 2),
}
# A DJED borrow under the 68/100 cap, by transaction-hash prefix.
DJED_CAPPED_BORROW = "1fbd5ab8ee4295fb"
# Candidate transactions scanned per discovered spend rule.
SCAN_LIMIT = 500

CONN = dict(
    host=os.environ.get("DBSYNC_HOST", "localhost"),
    port=int(os.environ.get("DBSYNC_PORT", "5432")),
    dbname=os.environ.get("DBSYNC_DB_NAME", "cexplorer"),
    user=os.environ.get("DBSYNC_USER", "cexplorer"),
    password=os.environ.get("DBSYNC_PASS", ""),
    connect_timeout=30,
)


# Policy and credential filters resolve their ids first (``= any(array(...))``): as
# a join or ``in`` subquery the planner reads ma_tx_out or tx_out whole.
POLICY_IDENTS = "array(select ma.id from multi_asset ma where ma.policy = any(%s))"
ADDRESSES = "array(select ad.id from address ad where ad.payment_cred = any(%s))"
# Fragments over record_sql(chain=True)'s aliases. Arguments are noted per fragment.
HOLDS_POLICY = f"""o.id = any(array(
    select mto.tx_out_id from ma_tx_out mto
    where mto.ident = any({POLICY_IDENTS})))"""  # [policy bytes]
AT_CREDENTIAL = f"o.address_id = any({ADDRESSES})"  # [credential bytes]
ALIVE_AT = "b.slot_no <= %s and (sb.slot_no is null or sb.slot_no > %s)"  # slot, slot
NO_TOKENS = "not exists (select 1 from ma_tx_out m where m.tx_out_id = o.id)"


def _b(hexes) -> list[bytes]:
    return [bytes.fromhex(h) for h in hexes]


def _records(cur, where: str, args: tuple, tip: int) -> list[dict[str, Any]]:
    """UTxO entries for every output matching ``where``, oldest first."""
    cur.execute(record_sql(chain=True) + " where " + where + " order by o.id", args)
    out = []
    for row in cur.fetchall():
        slot, block_index, block_time, spent_slot = row[6:]
        out.append(
            {
                **record_entry(cur, row),
                "slot": int(slot),
                "block_index": int(block_index),
                "block_time": int(block_time),
                "spent_slot": None if spent_slot is None else int(spent_slot),
                "live": slot <= tip and (spent_slot is None or spent_slot > tip),
            },
        )
    return out


def _ids(cur, where: str, args: tuple, *, first_only: bool = False) -> list[tuple]:
    """(tx_out id, datum hex) of the outputs matching ``where``, oldest first.

    The match is materialized before the sort: ``order by o.id limit 1`` over the
    plain query walks tx_out in primary-key order instead of using the filters.
    """
    sql = (
        "with m as materialized ("
        "select o.id, encode(d.bytes, 'hex') from tx_out o"
        " join tx t on t.id = o.tx_id join block b on b.id = t.block_id"
        " join address a on a.id = o.address_id"
        " left join datum d on d.id = o.inline_datum_id"
        " left join tx st on st.id = o.consumed_by_tx_id"
        " left join block sb on sb.id = st.block_id"
        " where "
        + where
        + ") select * from m order by 1"
        + (" limit 1" if first_only else "")
    )
    cur.execute(sql, args)
    return [(row[0], row[1] or "") for row in cur.fetchall()]


def _by_ids(cur, ids: list[int], tip: int) -> list[dict[str, Any]]:
    return _records(cur, "o.id = any(%s)", (ids,), tip) if ids else []


def _first(cur, where: str, args: tuple, tip: int, accept=None) -> dict[str, Any]:
    """The oldest output matching ``where`` whose datum hex passes ``accept``."""
    for tx_out_id, datum_hex in _ids(cur, where, args, first_only=accept is None):
        if accept is None or accept(datum_hex):
            (rec,) = _by_ids(cur, [tx_out_id], tip)
            return rec
    raise SystemExit(f"no output matches: {where}")


def _tx(cur, prefix: str) -> tuple[int, str, int]:
    """(tx id, full hash, slot) of the one transaction whose hash starts with prefix."""
    low = bytes.fromhex(prefix.ljust(64, "0"))
    high = bytes.fromhex(prefix.ljust(64, "f"))
    cur.execute(
        """select t.id, encode(t.hash, 'hex'), b.slot_no from tx t
           join block b on b.id = t.block_id
           where t.hash between %s and %s""",
        (low, high),
    )
    rows = cur.fetchall()
    if len(rows) != 1:
        raise SystemExit(f"tx prefix {prefix}: {len(rows)} matches")
    return rows[0]


def _out_ref(cur, tx_hash: str, index: int, tip: int) -> dict[str, Any]:
    tx_id, _, _ = _tx(cur, tx_hash)
    (rec,) = _records(cur, "o.tx_id = %s and o.index = %s", (tx_id, index), tip)
    return rec


def _fields(datum_hex: str) -> list | None:
    """The top-level fields of a bare-list datum; None for no datum or a Constr."""
    if not datum_hex:
        return None
    obj = cbor2.loads(bytes.fromhex(datum_hex))
    return list(obj) if isinstance(obj, list) else None


def _is_empty_constr(datum_hex: str) -> bool:
    if not datum_hex:
        return False
    obj = cbor2.loads(bytes.fromhex(datum_hex))
    return isinstance(obj, cbor2.CBORTag) and obj.tag == 121 and not list(obj.value)


def _length_is(*lengths: int):
    def accept(datum_hex: str) -> bool:
        fields = _fields(datum_hex)
        return fields is not None and len(fields) in lengths

    return accept


def _policies(rec: dict[str, Any]) -> set[str]:
    return {unit[:56] for unit in rec["assets"] if unit != "lovelace"}


def _market(deployment: LiqwidDeployment, name: str) -> MarketComponents:
    (market,) = [m for m in deployment.markets if m.name == name]
    return market


def _is_script_class(datum_hex: str) -> bool:
    """A CollateralParams datum whose collateral is a whole policy (``C0[policy]``)."""
    fields = _fields(datum_hex)
    return (
        fields is not None
        and len(fields) == 5
        and isinstance(fields[0], cbor2.CBORTag)
        and fields[0].tag == 121
    )


def _label(kind: str, rec: dict[str, Any], deployment: LiqwidDeployment) -> str | None:
    """None for a genuine record of ``kind``, else why the output is not one."""
    datum = rec["datum_cbor"]
    policies = _policies(rec)
    if kind == "oracle":
        return None if deployment.oracle_token_policy in policies else "oracle_decoy"
    if kind == "market_params":
        if all(deployment.market_of_params_policy(p) is None for p in policies):
            return "param_dust"
        return "parked_market_params" if _is_empty_constr(datum) else None
    if kind == "collateral_params":
        dead = any(p.startswith(DEAD_COLLATERAL_PARAMS_PREFIX) for p in policies)
        return "dead_policy_collateral_params" if dead else None
    if kind == "market_state":
        fields = _fields(datum)
        if fields is not None and len(fields) in (0, 27):
            return f"state_list{len(fields)}"
        return None
    if not datum:
        return "datumless_loan"
    return "parking_loan" if _is_empty_constr(datum) else None


def _stamp(kind: str, rec: dict[str, Any], deployment: LiqwidDeployment) -> dict:
    """``rec`` with its ``market_id``, ``generation`` and ``label``."""
    market_id = generation = None
    if kind == "loan":
        found = deployment.market_of_loan_validator(
            script_credential(rec["address"]) or "",
        )
        if found is not None:
            market_id, generation = found[0].market_id, found[1]
    elif kind != "oracle":
        lookup = {
            "market_state": deployment.market_of_state_policy,
            "market_params": deployment.market_of_params_policy,
            "collateral_params": deployment.market_of_collateral_params_policy,
        }[kind]
        found_ids = {m.market_id for m in map(lookup, _policies(rec)) if m is not None}
        if len(found_ids) == 1:
            (market_id,) = found_ids
    label = _label(kind, rec, deployment)
    return {**rec, "market_id": market_id, "generation": generation, "label": label}


def _kind_lists(
    entries: dict[str, list[dict[str, Any]]],
    deployment: LiqwidDeployment,
) -> dict[str, list[dict[str, Any]]]:
    """Every kind's entries stamped, de-duplicated by out-ref and sorted."""
    out = {}
    for kind, recs in entries.items():
        unique = {rec["out_ref"]: _stamp(kind, rec, deployment) for rec in recs}
        out[kind] = [unique[ref] for ref in sorted(unique)]
    return out


def _parameter_records(cur, deployment: LiqwidDeployment, tip: int) -> dict:
    mp = _b(m.market_params_policy for m in deployment.markets)
    cp = _b(m.collateral_params_policy for m in deployment.markets)
    param = _b([deployment.param_credential])
    market_params = [
        _out_ref(cur, tx_hash, index, tip)
        for tx_hash, index in MARKET_PARAMS_LAYOUTS.values()
    ]
    for length, rec in zip(MARKET_PARAMS_LAYOUTS, market_params):
        if len(_fields(rec["datum_cbor"]) or ()) != length:
            raise SystemExit(f"{rec['out_ref']} is not a list{length} MarketParams")
    parked = _out_ref(cur, *PARKED_MARKET_PARAMS, tip)
    if not _is_empty_constr(parked["datum_cbor"]):
        raise SystemExit(f"{parked['out_ref']} is not a parked MarketParams")
    market_params.append(parked)
    market_params.append(
        _first(cur, f"{AT_CREDENTIAL} and {NO_TOKENS}", (param,), tip),
    )
    market_params += _records(
        cur, f"{HOLDS_POLICY} and {ALIVE_AT}", (mp, tip, tip), tip
    )

    ada = _market(deployment, "Ada")
    collateral_params = _records(
        cur,
        f"{HOLDS_POLICY} and {ALIVE_AT}",
        (cp, tip, tip),
        tip,
    )
    collateral_params.append(
        _first(cur, HOLDS_POLICY, (_b([ada.collateral_params_policy]),), tip),
    )
    dead = DEAD_COLLATERAL_PARAMS_PREFIX
    collateral_params.append(
        _first(
            cur,
            f"""{AT_CREDENTIAL} and o.id in (
                select mto.tx_out_id from ma_tx_out mto
                join multi_asset ma on ma.id = mto.ident
                where ma.policy between %s and %s)""",
            (
                param,
                bytes.fromhex(dead.ljust(56, "0")),
                bytes.fromhex(dead.ljust(56, "f")),
            ),
            tip,
        ),
    )
    collateral_params.append(
        _first(cur, HOLDS_POLICY, (cp,), tip, _is_script_class),
    )
    return {"market_params": market_params, "collateral_params": collateral_params}


def _market_state_records(cur, deployment: LiqwidDeployment, tip: int) -> list:
    states = []
    for name, validator in LAUNCH_STATE_VALIDATORS.items():
        market = _market(deployment, name)
        if validator not in market.market_state_validators:
            raise SystemExit(f"{name}: launch validator {validator} not bundled")
        where = f"{AT_CREDENTIAL} and {HOLDS_POLICY}"
        args = (_b([validator]), _b([market.market_state_policy]))
        states.append(_first(cur, where, args, tip, _length_is(11)))
        if name == "Ada":
            # The Ada NFT also sat on a list0 and a list27 output at its launch
            # validator; neither is a MarketState record.
            states.append(_first(cur, where, args, tip, _length_is(0)))
            states.append(_first(cur, where, args, tip, _length_is(27)))
    policies = _b(m.market_state_policy for m in deployment.markets)
    states += _records(cur, f"{HOLDS_POLICY} and {ALIVE_AT}", (policies, tip, tip), tip)
    return states


def _parameters_alive_at(cur, deployment, states: list, tip: int) -> dict:
    """MarketParams and CollateralParams alive at every historical MarketState."""
    out: dict[str, list] = {"market_params": [], "collateral_params": []}
    for state in states:
        if state["live"]:
            continue
        (market,) = {
            m
            for m in map(deployment.market_of_state_policy, _policies(state))
            if m is not None
        }
        slot = state["slot"]
        where = (
            f"{HOLDS_POLICY} and b.slot_no <= %s"
            " and (sb.slot_no is null or sb.slot_no >= %s)"
        )
        for kind, policy in (
            ("market_params", market.market_params_policy),
            ("collateral_params", market.collateral_params_policy),
        ):
            out[kind] += _records(cur, where, (_b([policy]), slot, slot), tip)
    return out


def _loan_records(cur, deployment: LiqwidDeployment, tip: int) -> list:
    creds = sorted({v for m in deployment.markets for v in m.loan_validators})
    loan_tokens = deployment.loan_token_policies
    script_class = _script_class_policy(cur, deployment)
    cur.execute(
        f"""select o.id, encode(a.payment_cred, 'hex'),
                  encode(substring(d.bytes from 1 for 3), 'hex'),
                  o.inline_datum_id is null and o.data_hash is null,
                  array_remove(array_agg(
                      encode(ma.policy, 'hex') || encode(ma.name, 'hex')), null)
           from tx_out o
           join address a on a.id = o.address_id
           left join datum d on d.id = o.inline_datum_id
           left join ma_tx_out mto on mto.tx_out_id = o.id
           left join multi_asset ma on ma.id = mto.ident
           where o.address_id = any({ADDRESSES})
           group by o.id, a.payment_cred, d.bytes
           order by o.id""",
        (_b(creds),),
    )
    chosen: dict[str, int] = {}
    for tx_out_id, cred, head, no_datum, units in cur.fetchall():
        _, generation = deployment.market_of_loan_validator(cred)
        tokens = [u for u in units if u[:56] in loan_tokens]
        others = [u for u in units if u[:56] not in loan_tokens]
        qtokens = [u for u in others if deployment.market_of_qtoken(u[:56])]
        is_list = head is not None and head[:2] in ("85", "9f")
        categories = []
        if no_datum:
            categories.append("datumless")
        if head == "d87980":
            categories.append("parking")
        if is_list and tokens:
            if generation == "legacy" and head[:2] == "9f":
                categories.append("legacy_indefinite")
            if generation == "new":
                definite = "new_definite" if head[:2] == "85" else "new_indefinite"
                categories.append(definite)
            if len(qtokens) == len(others) and len(others) in (0, 1, 5):
                categories.append(f"collateral_{len(others)}")
            if any(u[56:] in ("30", "31") for u in tokens):
                categories.append("name_30_31")
            if any(u[:56] == script_class for u in others):
                categories.append("script_class")
        for category in categories:
            chosen.setdefault(category, tx_out_id)
    wanted = {
        "datumless",
        "parking",
        "legacy_indefinite",
        "new_definite",
        "new_indefinite",
        "collateral_0",
        "collateral_1",
        "collateral_5",
        "name_30_31",
        "script_class",
    }
    if wanted - set(chosen):
        raise SystemExit(f"loan categories not found: {sorted(wanted - set(chosen))}")
    loans = _by_ids(cur, sorted(set(chosen.values())), tip)
    loans += _records(
        cur, f"{AT_CREDENTIAL} and {ALIVE_AT}", (_b(creds), tip, tip), tip
    )
    return loans


def _script_class_policy(cur, deployment: LiqwidDeployment) -> str:
    """The policy a script-class (``C0[policy]``) CollateralParams record names."""
    cp = _b(m.collateral_params_policy for m in deployment.markets)
    for _, datum_hex in _ids(cur, HOLDS_POLICY, (cp,)):
        if _is_script_class(datum_hex):
            return plutus_fields(_fields(datum_hex)[0])[0].hex()
    raise SystemExit("no script-class CollateralParams record")


def _oracle_records(cur, deployment: LiqwidDeployment, tip: int) -> list:
    roles = {v.role: v.script_hash for v in deployment.oracle_validators}
    token = _b([deployment.oracle_token_policy])
    feeds = []
    with_token = f"{AT_CREDENTIAL} and {HOLDS_POLICY}"
    feeds.append(_first(cur, with_token, (_b([roles["v1"]]), token), tip))
    feeds.append(_first(cur, with_token, (_b([roles["hard_caps"]]), token), tip))
    pegs = (_b([roles["pegs"]]), token)
    feeds.append(_first(cur, with_token, pegs, tip, _length_is(5)))
    feeds.append(_first(cur, with_token, pegs, tip, _length_is(6)))

    def policy_wide(datum_hex: str) -> bool:
        fields = _fields(datum_hex)
        return (
            bool(fields)
            and isinstance(fields[1], cbor2.CBORTag)
            and (fields[1].tag == 121)
        )

    feeds.append(
        _first(
            cur,
            f"{with_token} and substring(d.bytes from 32 for 2) = %s",
            (_b(roles.values()), token, b"\xd8\x79"),
            tip,
            policy_wide,
        ),
    )
    decoys = [
        rec
        for rec in _records(
            cur,
            f"{AT_CREDENTIAL} and {NO_TOKENS} and {ALIVE_AT}",
            (_b([roles["hard_caps"]]), tip, tip),
            tip,
        )
        if _length_is(5, 6)(rec["datum_cbor"])
    ]
    if len(decoys) != 4:
        raise SystemExit(f"expected 4 token-less feed decoys, found {len(decoys)}")
    feeds += decoys
    feeds += _records(
        cur,
        f"{with_token} and {ALIVE_AT}",
        (_b(roles.values()), token, tip, tip),
        tip,
    )
    return feeds


def _loan_creds(deployment: LiqwidDeployment) -> list[bytes]:
    return _b(sorted({v for m in deployment.markets for v in m.loan_validators}))


def _loan_state(rec: dict[str, Any], deployment: LiqwidDeployment) -> tuple | None:
    """(market_id, owner hex, loan state) of a genuine loan output; else None."""
    fields = _fields(rec["datum_cbor"])
    found = deployment.market_of_loan_validator(
        script_credential(rec["address"]) or "",
    )
    if fields is None or len(fields) != 5 or found is None:
        return None
    collateral = tuple(
        sorted(
            (unit, quantity)
            for unit, quantity in rec["assets"].items()
            if unit != "lovelace" and unit[:56] not in deployment.loan_token_policies
        ),
    )
    state = (fields[1], fields[2], fields[3], fields[4], collateral)
    return found[0].market_id, fields[0].hex(), state


def _reference_successors(spent: list, created: list, deployment) -> dict[str, str]:
    """Spent out-ref -> created out-ref within one transaction, per market."""
    spent_keys = {
        rec["out_ref"]: key
        for rec in spent
        if (key := _loan_state(rec, deployment)) is not None
    }
    created_keys = {
        rec["out_ref"]: key
        for rec in created
        if (key := _loan_state(rec, deployment)) is not None
    }
    links: dict[str, str] = {}
    for part in (1, 2):  # 1: owner, 2: loan state
        free_spent = {r: k for r, k in spent_keys.items() if r not in links}
        taken = set(links.values())
        free_created = {r: k for r, k in created_keys.items() if r not in taken}
        spent_count = Counter((k[0], k[part]) for k in free_spent.values())
        created_count = Counter((k[0], k[part]) for k in free_created.values())
        by_value = {(k[0], k[part]): r for r, k in free_created.items()}
        for ref, key in sorted(free_spent.items()):
            value = (key[0], key[part])
            if spent_count[value] == 1 and created_count[value] == 1:
                links[ref] = by_value[value]
    return links


def _spend(cur, deployment, rule: str, tx: tuple, tip: int) -> dict[str, Any]:
    tx_id, tx_hash, slot = tx
    creds = _loan_creds(deployment)
    spent = [
        _stamp("loan", rec, deployment)
        for rec in _records(
            cur,
            f"o.consumed_by_tx_id = %s and {AT_CREDENTIAL}",
            (tx_id, creds),
            tip,
        )
    ]
    created = [
        _stamp("loan", rec, deployment)
        for rec in _records(
            cur, f"o.tx_id = %s and {AT_CREDENTIAL}", (tx_id, creds), tip
        )
    ]
    cur.execute(
        """select encode(sa.script_hash, 'hex') from withdrawal w
           join stake_address sa on sa.id = w.addr_id
           where w.tx_id = %s and sa.script_hash is not null
           order by 1""",
        (tx_id,),
    )
    withdrawals = [row[0] for row in cur.fetchall()]
    cur.execute(
        """select encode(ma.policy, 'hex') || encode(ma.name, 'hex'), m.quantity
           from ma_tx_mint m
           join multi_asset ma on ma.id = m.ident
           where m.tx_id = %s
           order by 1""",
        (tx_id,),
    )
    mints = [[unit, int(quantity)] for unit, quantity in cur.fetchall()]
    minted: dict[str, int] = {}
    for unit, quantity in mints:
        minted[unit[:56]] = minted.get(unit[:56], 0) + quantity
    links = _reference_successors(spent, created, deployment)
    with_successor, without = RULE_TRANSITIONS[rule]
    expected = {
        rec["out_ref"]: {
            "successor": links.get(rec["out_ref"]),
            "transition": with_successor if rec["out_ref"] in links else without,
        }
        for rec in spent
    }
    return {
        "tx_hash": tx_hash,
        "slot": int(slot),
        "rule": rule,
        "expected": expected,
        "spent": spent,
        "created": created,
        "withdrawals": withdrawals,
        "mints": mints,
        "minted": minted,
        "burned_any": sorted({unit[:56] for unit, q in mints if q < 0}),
    }


# Rules whose evidence is one market's scripts: the spend must touch one market.
SINGLE_MARKET_RULES = {
    "partial_liquidation",
    "new_modification",
    "full_repay",
    "legacy_liquidation",
    "legacy_repay",
    "legacy_flip",
}


def _spend_problem(spend: dict[str, Any], deployment: LiqwidDeployment) -> str | None:
    """Why ``spend`` does not show its rule's on-chain evidence; None if it does."""
    rule, spent = spend["rule"], spend["spent"]
    if not spent or any(_loan_state(rec, deployment) is None for rec in spent):
        return "spends no loan, or an output that is not a loan record"
    markets = [deployment.market(m) for m in sorted({r["market_id"] for r in spent})]
    if rule in SINGLE_MARKET_RULES and len(markets) != 1:
        return f"spends loans of {len(markets)} markets"
    successors = {ref: o["successor"] for ref, o in spend["expected"].items()}
    all_linked = all(successors.values())
    none_linked = not any(successors.values())
    withdrawn = set(spend["withdrawals"])
    borrow_staking = {s for m in markets for s in m.borrow_staking}
    liquidation_staking = {s for m in markets for s in m.liquidation_staking}
    liquidation_minted = any(
        m.liquidation_policy and spend["minted"].get(m.liquidation_policy, 0) > 0
        for m in markets
    )
    no_staking = not withdrawn & (borrow_staking | liquidation_staking)
    admin = no_staking and not liquidation_minted
    # Rules 4 and 5 come before the admin rules: a loan-token burn without a
    # successor is a repay, a burn re-minted on the same policy with one a flip.
    loan_tokens = {p for m in markets for p in m.loan_token_policies}
    tokens_burned = bool(loan_tokens & set(spend["burned_any"]))
    flipped = any(
        p in spend["burned_any"] and spend["minted"].get(p, 0) >= 0 for p in loan_tokens
    )
    created = {rec["out_ref"]: rec for rec in spend["created"]}
    pairs = [
        (
            _loan_state(rec, deployment),
            _loan_state(created[successors[rec["out_ref"]]], deployment),
            rec["generation"],
            created[successors[rec["out_ref"]]]["generation"],
        )
        for rec in spent
        if successors[rec["out_ref"]]
    ]
    owners = Counter(_loan_state(rec, deployment)[:2] for rec in spent)
    legacy = all(rec["generation"] == "legacy" for rec in spent)
    borrow = markets[0].borrow_policy
    burned = borrow in spend["burned_any"]
    checks = {
        "partial_liquidation": all_linked and bool(withdrawn & liquidation_staking),
        "new_modification": all_linked
        and bool(withdrawn & borrow_staking)
        and not withdrawn & liquidation_staking,
        "full_repay": none_linked
        and bool(withdrawn & borrow_staking)
        and not withdrawn & liquidation_staking,
        "legacy_liquidation": legacy
        and all_linked
        and liquidation_minted
        and no_staking,
        "legacy_repay": legacy and none_linked and burned and admin,
        "legacy_flip": legacy
        and all_linked
        and len(spent) == 1
        and burned
        and spend["minted"].get(borrow) == 0
        and admin,
        "admin_migration": admin
        and all_linked
        and not flipped
        and all(old_gen != new_gen for _, _, old_gen, new_gen in pairs),
        "admin_reown": admin
        and all_linked
        and not flipped
        and all(old[1] != new[1] for old, new, _, _ in pairs),
        "same_generation_rewrite": admin
        and all_linked
        and not spend["burned_any"]
        and not any(spend["minted"].values())
        and all(
            old[1] == new[1] and old_gen == new_gen
            for old, new, old_gen, new_gen in pairs
        ),
        "parking": admin
        and none_linked
        and not tokens_burned
        and any(rec["label"] == "parking_loan" for rec in spend["created"]),
        "multi_loan": admin
        and all_linked
        and not spend["mints"]
        and max(owners.values()) >= 2,
    }
    return None if checks[rule] else f"no {rule} evidence"


def _named_spend(cur, deployment, rule: str, tip: int) -> dict[str, Any]:
    spend = _spend(cur, deployment, rule, _tx(cur, NAMED_SPENDS[rule]), tip)
    problem = _spend_problem(spend, deployment)
    if problem is not None:
        raise SystemExit(f"{rule} {spend['tx_hash']}: {problem}")
    return spend


def _discovered_spend(cur, deployment, rule: str, sql: str, args: tuple, tip: int):
    """The oldest candidate transaction that shows ``rule``'s evidence."""
    cur.execute(sql + f" limit {SCAN_LIMIT}", args)
    for tx in cur.fetchall():
        spend = _spend(cur, deployment, rule, tx, tip)
        if _spend_problem(spend, deployment) is None:
            return spend
    raise SystemExit(f"{rule}: no qualifying transaction in {SCAN_LIMIT} candidates")


_TX = (
    "select t.id, encode(t.hash, 'hex'), b.slot_no from tx t"
    " join block b on b.id = t.block_id where "
)
_MINTS = """t.id = any(array(select m.tx_id from ma_tx_mint m
    where m.ident = any(array(select ma.id from multi_asset ma where ma.policy = %s))
    and m.quantity {sign} 0))"""
_SPENDS_AT = f"""exists (select 1 from tx_out o
    where o.consumed_by_tx_id = t.id and {AT_CREDENTIAL})"""
_CREATES_AT = f"""exists (select 1 from tx_out o
    where o.tx_id = t.id and {AT_CREDENTIAL})"""
# Datum hashes (blake2b-256) of the empty constructor, definite and indefinite.
_EMPTY_CONSTR_HASHES = [
    hashlib.blake2b(datum, digest_size=32).digest()
    for datum in (b"\xd8\x79\x80", b"\xd8\x79\x9f\xff")
]


def _spends(cur, deployment: LiqwidDeployment, tip: int) -> list[dict[str, Any]]:
    ada = _market(deployment, "Ada")
    legacy = _b(ada.legacy_loan_validators)
    ada_loans = _b(ada.loan_validators)
    borrow = bytes.fromhex(ada.borrow_policy)
    liquidation = bytes.fromhex(ada.liquidation_policy)
    creds = _loan_creds(deployment)
    burn = _MINTS.format(sign="<")
    mint = _MINTS.format(sign=">")
    searches = {
        "legacy_liquidation": (
            _TX + f"{mint} and {_SPENDS_AT} order by t.id",
            (liquidation, legacy),
        ),
        "legacy_repay": (
            _TX + f"{burn} and {_SPENDS_AT} and not {_CREATES_AT} order by t.id",
            (borrow, legacy, ada_loans),
        ),
        "legacy_flip": (
            _TX + f"{burn} and {mint} and {_SPENDS_AT} order by t.id",
            (borrow, borrow, legacy),
        ),
        "parking": (
            _TX
            + f"""t.id = any(array(select o.tx_id from tx_out o
                 where {AT_CREDENTIAL} and o.inline_datum_id = any(array(
                     select dt.id from datum dt where dt.hash = any(%s)))))
               and {_SPENDS_AT} order by t.id""",
            (creds, _EMPTY_CONSTR_HASHES, creds),
        ),
        "multi_loan": (
            _TX
            + f"""t.id = any(array(select o.consumed_by_tx_id from tx_out o
                 where {AT_CREDENTIAL} and o.consumed_by_tx_id is not null
                 group by o.consumed_by_tx_id having count(*) >= 2))
               and not exists (select 1 from withdrawal w where w.tx_id = t.id)
               and not exists (select 1 from ma_tx_mint m where m.tx_id = t.id)
               order by (select count(*) from tx_out o
                         where o.consumed_by_tx_id = t.id), t.id""",
            (creds,),
        ),
    }
    spends = [_named_spend(cur, deployment, rule, tip) for rule in NAMED_SPENDS]
    for rule, (sql, args) in searches.items():
        spends.append(_discovered_spend(cur, deployment, rule, sql, args, tip))
    return sorted(spends, key=lambda spend: list(SPEND_RULES).index(spend["rule"]))


def _principal_added(cur, tx_id: int, creds: list[bytes]) -> int:
    total = 0
    for column, sign in (("o.tx_id", 1), ("o.consumed_by_tx_id", -1)):
        cur.execute(
            "select encode(d.bytes, 'hex') from tx_out o"
            " join address a on a.id = o.address_id"
            " left join datum d on d.id = o.inline_datum_id"
            f" where {column} = %s and a.payment_cred = any(%s)",
            (tx_id, creds),
        )
        for (datum_hex,) in cur.fetchall():
            fields = _fields(datum_hex or "")
            if fields is not None and len(fields) == 5:
                total += sign * int(fields[1])
    return total


def _alive_before(cur, policy: str, tx_id: int, tip: int) -> dict[str, Any]:
    """The output holding ``policy`` that was unspent when ``tx_id`` landed."""
    (rec,) = _records(
        cur,
        f"{HOLDS_POLICY} and o.tx_id < %s"
        " and (o.consumed_by_tx_id is null or o.consumed_by_tx_id > %s)",
        (_b([policy]), tx_id, tx_id),
        tip,
    )
    return rec


def _max_borrow(cur, deployment, market, tx: tuple, borrowed: int, tip: int) -> dict:
    tx_id, tx_hash, slot = tx
    state = _alive_before(cur, market.market_state_policy, tx_id, tip)
    params = _alive_before(cur, market.market_params_policy, tx_id, tip)
    return {
        "tx_hash": tx_hash,
        "slot": int(slot),
        "market_id": market.market_id,
        "state": _stamp("market_state", state, deployment),
        "params": _stamp("market_params", params, deployment),
        "borrowed": borrowed,
    }


def _headroom(state: dict[str, Any], params: dict[str, Any], *, interest=False) -> int:
    s = _fields(state["datum_cbor"])
    p = _fields(params["datum_cbor"])
    if s is None or len(s) != 11 or p is None:
        raise SystemExit(f"{state['out_ref']} / {params['out_ref']}: not records")
    cash, principal = int(s[0]), int(s[3]) + (int(s[4]) if interest else 0)
    cap_field = p[32] if len(p) >= 34 else None
    if not isinstance(cap_field, cbor2.CBORTag) or cap_field.tag != 121:
        return cash
    num, den = plutus_fields(plutus_fields(cap_field)[0])
    room = math.floor(Fraction(int(num), int(den)) * (cash + principal) - principal)
    return max(0, min(cash, room))


def _max_borrows(cur, deployment: LiqwidDeployment, tip: int) -> list[dict]:
    entries = []
    for name, (low, high, (least, most), count) in CAPPED_BORROW_WINDOWS.items():
        market = _market(deployment, name)
        creds = _b(market.loan_validators)
        cur.execute(
            f"""select distinct t.id, encode(t.hash, 'hex'), b.slot_no
               from tx_out o
               join tx t on t.id = o.tx_id
               join block b on b.id = t.block_id
               where {AT_CREDENTIAL} and b.slot_no between %s and %s
               order by t.id""",
            (creds, low, high),
        )
        found = []
        for tx in cur.fetchall():
            borrowed = _principal_added(cur, tx[0], creds)
            if least <= borrowed <= most:
                found.append(_max_borrow(cur, deployment, market, tx, borrowed, tip))
        if len(found) != count:
            raise SystemExit(f"{name}: {len(found)} capped borrows, expected {count}")
        entries += found
    djed = _market(deployment, "DJED")
    tx = _tx(cur, DJED_CAPPED_BORROW)
    borrowed = _principal_added(cur, tx[0], _b(djed.loan_validators))
    entries.append(_max_borrow(cur, deployment, djed, tx, borrowed, tip))
    _check_capped_borrows(entries, deployment)
    return entries


def _check_capped_borrows(entries: list[dict], deployment: LiqwidDeployment) -> None:
    """Every capped borrow fits the principal-only headroom of its batched state."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        groups.setdefault((entry["market_id"], entry["state"]["out_ref"]), []).append(
            entry,
        )
    best: dict[str, Fraction] = {}
    for (market_id, state_ref), group in groups.items():
        name = deployment.market(market_id).name
        total = sum(entry["borrowed"] for entry in group)
        room = _headroom(group[0]["state"], group[0]["params"])
        with_interest = _headroom(group[0]["state"], group[0]["params"], interest=True)
        print(
            f"capped borrow {name} state {state_ref}: borrowed {total}"
            f" headroom principal-only {room} with-interest {with_interest}",
        )
        if total > room:
            raise SystemExit(f"{name}: borrowed {total} over the headroom {room}")
        best[name] = max(best.get(name, Fraction(0)), Fraction(total, room))
    for name in CAPPED_BORROW_WINDOWS:
        if best[name] < Fraction(999, 1000):
            raise SystemExit(f"{name}: no borrow within 0.1% of the headroom")


def _print_live_headroom(records: dict, deployment: LiqwidDeployment) -> None:
    """Principal-only borrow headroom per live market (the offers' borrow amount)."""
    newest_params: dict[str, dict] = {}
    for rec in records["market_params"]:
        if rec["live"] and rec["label"] is None:
            current = newest_params.get(rec["market_id"])
            if current is None or rec["slot"] > current["slot"]:
                newest_params[rec["market_id"]] = rec
    for rec in records["market_state"]:
        if not rec["live"] or rec["label"] is not None:
            continue
        params = newest_params[rec["market_id"]]
        name = deployment.market(rec["market_id"]).name
        print(
            f"headroom {name}: principal-only {_headroom(rec, params)}"
            f" with-interest {_headroom(rec, params, interest=True)}",
        )


_MARKETS_QUERY = """
query Markets($input: MarketsInput) {
  liqwid { data { markets(input: $input) {
    totalCount
    results {
      id
      receiptAsset { policyId }
      borrowAPR
      supplyAPY
      utilization
      exchangeRate
      liquidity
      parameters { borrowCap }
    }
  } } }
}
"""
_LOANS_QUERY = """
query Loans($input: LoansInput) {
  liqwid { data { loans(input: $input) {
    totalCount
    results { id debt interest healthFactor LTV }
  } } }
}
"""


def _graphql_page(query: str, field: str, per_page: int) -> dict[str, Any]:
    response = requests.post(
        API_URL,
        json={"query": query, "variables": {"input": {"page": 0, "perPage": per_page}}},
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise SystemExit(f"{field}: {body['errors']}")
    return body["data"]["liqwid"]["data"][field]


def _graphql_rows(query: str, field: str) -> list[dict[str, Any]]:
    """Every row of a paged ``liqwid.data.<field>`` query, de-duplicated by id.

    The API's row order is not stable between requests, so consecutive pages can
    overlap and skip rows; every row is read in one page sized by ``totalCount``.
    """
    size = int(_graphql_page(query, field, 1)["totalCount"])
    data = _graphql_page(query, field, size)
    total = int(data["totalCount"])
    rows = {row["id"]: row for row in data["results"]}
    if len(rows) != total:
        raise SystemExit(f"{field}: {len(rows)} rows, API total {total}")
    return list(rows.values())


def _api_snapshot(deployment: LiqwidDeployment, slot: int) -> dict[str, Any]:
    markets = {}
    for row in _graphql_rows(_MARKETS_QUERY, "markets"):
        market = deployment.market_of_qtoken(row["receiptAsset"]["policyId"])
        if market is None:
            raise SystemExit(f"API market {row['id']}: qToken not in the snapshot")
        markets[market.market_id] = {
            "borrowAPR": row["borrowAPR"],
            "supplyAPR": row["supplyAPY"],
            "utilization": row["utilization"],
            "exchangeRate": row["exchangeRate"],
            "liquidity": row["liquidity"],
            "borrowCap": (row.get("parameters") or {}).get("borrowCap"),
        }
    loans = {}
    for row in _graphql_rows(_LOANS_QUERY, "loans"):
        tx_hash, index = row["id"].rsplit("-", 1)
        loans[f"{tx_hash}#{int(index)}"] = {
            "debt": row["debt"],
            "interest": row["interest"],
            "healthFactor": row["healthFactor"],
            "LTV": row["LTV"],
        }
    return {
        "slot": slot,
        "loans": dict(sorted(loans.items())),
        "markets": dict(sorted(markets.items())),
    }


def _check_api(records: dict, api: dict, deployment: LiqwidDeployment) -> None:
    live_loans = {
        rec["out_ref"]
        for rec in records["loan"]
        if rec["live"] and rec["label"] is None
    }
    only_chain = live_loans - set(api["loans"])
    only_api = set(api["loans"]) - live_loans
    if only_chain or only_api:
        raise SystemExit(
            f"loans at slot {api['slot']}: {len(only_chain)} only on chain,"
            f" {len(only_api)} only in the API; re-run the capture",
        )
    if set(api["markets"]) != {m.market_id for m in deployment.markets}:
        raise SystemExit("API markets differ from the bundled deployment")


def capture() -> tuple[dict[str, Any], dict[str, Any]]:
    deployment = default_deployment()
    conn = psycopg.connect(**CONN)
    cur = conn.cursor()
    cur.execute("select max(slot_no) from block")
    (tip,) = cur.fetchone()
    tip = int(tip)
    api = _api_snapshot(deployment, tip)
    entries = _parameter_records(cur, deployment, tip)
    entries["market_state"] = _market_state_records(cur, deployment, tip)
    alive = _parameters_alive_at(cur, deployment, entries["market_state"], tip)
    for kind, recs in alive.items():
        entries[kind] += recs
    entries["loan"] = _loan_records(cur, deployment, tip)
    entries["oracle"] = _oracle_records(cur, deployment, tip)
    records: dict[str, Any] = _kind_lists(entries, deployment)
    records["spends"] = _spends(cur, deployment, tip)
    records["max_borrows"] = _max_borrows(cur, deployment, tip)
    conn.close()
    _check_api(records, api, deployment)
    _print_live_headroom(records, deployment)
    return records, api


if __name__ == "__main__":
    records, api = capture()
    payload = (json.dumps(records, indent=2) + "\n").encode()
    (HERE / "records.json.gz").write_bytes(gzip.compress(payload, mtime=0))
    (HERE / "api_snapshot.json").write_text(json.dumps(api, indent=2) + "\n")
    for key, value in records.items():
        print(f"{key}: {len(value)}")
    print(f"api slot {api['slot']}: {len(api['loans'])} loans, {len(api['markets'])}")
