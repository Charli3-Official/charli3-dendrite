"""Dev-only: regenerate the bundled Liqwid V2 registry snapshot.

Fetches the public registry (``deployment.REGISTRY_URL``), or reads a saved copy
given with ``--from``, and writes
``src/charli3_dendrite/lending/liqwid/registry_v6.json``:

- ``scriptInfos``: the mainnet entries whose (componentName, type) is in
  ``deployment.MARKET_FAMILIES`` (tags ``liqwidMainnet`` / ``liqwidMainnetLegacy``,
  with a market) or ``deployment.ORACLE_FAMILIES`` / ``ORACLE_TOKEN_FAMILY`` (tag
  ``oracleMainnet``), each reduced to tag, market, componentName, name, type and
  scriptHash, sorted by those keys;
- ``other.liqwidMainnetParams``: each market's ``marketDecimal`` and
  ``underlyingClass``;
- ``handKept``: ``HAND_KEPT`` below, the facts the registry does not list;
- ``_comment`` / ``_sourceSha256``: provenance (the loader ignores ``_`` keys).

Run: PYTHONPATH=src python tests/lending/liqwid/fixtures/_refresh_registry.py
[--from saved_registry.json]
Needs network access unless --from is given. Not run in CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from charli3_dendrite.lending.liqwid import deployment

OUT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "charli3_dendrite"
    / "lending"
    / "liqwid"
    / deployment.REGISTRY_RESOURCE
)
KEPT_KEYS = ("tag", "market", "componentName", "name", "type", "scriptHash")
KEPT_PARAMS = ("marketDecimal", "underlyingClass")

# Not in the registry. The param address holds every MarketParams and
# CollateralParams record. The launch MarketState validators held each market's
# state until the 2023-07-19 migration; outputs there carry the market's NFT.
HAND_KEPT = {
    "paramCredential": "b575da974ec756ba2afdccdc07cbd1839acaf0a24c4f80afd81d3fb6",
    "launchMarketStateValidators": {
        "Ada": "0fd1b854729d2e29290d47fe2173319aae3c4c885a955dc203f72759",
        "DJED": "63d65418f841e9901d5ee634cc07377cca2be7fb2b797e3f0e0d8334",
        "IUSD": "9885c2bc619cb1e09d63c59eebef3c27e7b796cdca3ff89fdbfb4fa1",
        "SHEN": "89ea1358d8147feb5ef1875fbad449f65719ced6102ce1600bf093a7",
    },
}


def _kept(entry: dict) -> bool:
    family = (entry.get("componentName"), entry.get("type"))
    if entry.get("tag") == deployment.ORACLE_TAG:
        return (
            family in deployment.ORACLE_FAMILIES
            or family == deployment.ORACLE_TOKEN_FAMILY
        )
    return (
        entry.get("tag") in deployment.MARKET_TAGS
        and "market" in entry
        and family in deployment.MARKET_FAMILIES
    )


def trim(raw: bytes) -> dict:
    """The snapshot document for a raw registry download."""
    registry = json.loads(raw)
    infos = [
        {key: entry[key] for key in KEPT_KEYS if key in entry}
        for entry in registry["scriptInfos"]
        if _kept(entry)
    ]
    infos.sort(key=lambda entry: tuple(entry.get(key, "") for key in KEPT_KEYS))
    params = registry["other"]["liqwidMainnetParams"]
    return {
        "_comment": (
            "Trimmed mainnet snapshot of " + deployment.REGISTRY_URL + ", written"
            " by tests/lending/liqwid/fixtures/_refresh_registry.py; handKept"
            " holds facts the registry does not list."
        ),
        "_sourceSha256": hashlib.sha256(raw).hexdigest(),
        "scriptInfos": infos,
        "other": {
            "liqwidMainnetParams": {
                name: {key: params[name][key] for key in KEPT_PARAMS}
                for name in sorted(params)
            },
        },
        "handKept": HAND_KEPT,
    }


def main() -> None:
    """Fetch (or read) the registry and write the trimmed snapshot."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="source", type=Path)
    args = parser.parse_args()
    if args.source is not None:
        raw = args.source.read_bytes()
    else:
        # The registry host refuses urllib's default User-Agent (HTTP 403).
        request = urllib.request.Request(
            deployment.REGISTRY_URL,
            headers={"User-Agent": "charli3-dendrite"},
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            raw = resp.read()
    document = trim(raw)
    deployment.load_deployment(document)
    OUT.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {OUT} ({len(document['scriptInfos'])} script entries)")


if __name__ == "__main__":
    main()
