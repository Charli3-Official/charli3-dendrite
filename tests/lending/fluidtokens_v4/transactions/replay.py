"""Shared helpers for the V4 builder tests: fixtures, forward builds and Ogmios."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pycardano import Address
from pycardano import RawCBOR
from pycardano import Transaction
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_value
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    captured_config,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import OracleWitness
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor

FIXTURES = Path(__file__).parent / "fixtures"
_PURPOSE = {0: "spend", 1: "mint", 2: "cert", 3: "reward"}

needs_ogmios = pytest.mark.skipif(
    not os.environ.get("OGMIOS_HOST"),
    reason="needs OGMIOS_HOST",
)
needs_dbsync = pytest.mark.skipif(
    not (os.environ.get("DBSYNC_HOST") and os.environ.get("OGMIOS_HOST")),
    reason="needs DBSYNC_* and OGMIOS_HOST",
)


def fixture(name: str) -> dict[str, Any]:
    """A captured transaction fixture."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def reference_script(label: str) -> Utxo:
    """A captured reference-script UTxO from ``reference_scripts.json``."""
    return utxo_from_dict(fixture("reference_scripts")[label])


def captured_scripts(fix: dict[str, Any]) -> ConfigDatum:
    """The config datum a capture referenced, which names the scripts it ran."""
    return captured_config(utxo_from_dict(u) for u in fix["ref_inputs"])[1]


def captured_purposes(fix: dict[str, Any]) -> list[str]:
    """The sorted redeemer purposes of a capture, as Ogmios names them."""
    return sorted(
        "withdraw" if r["purpose"] == "reward" else r["purpose"]
        for r in fix["redeemers"]
    )


def fixture_utxos(fix: dict[str, Any]) -> list[Utxo]:
    """A capture's spent inputs and reference inputs."""
    return [utxo_from_dict(u) for u in fix["inputs"] + fix["ref_inputs"]]


def at_minimum_ada(utxo: Utxo) -> Utxo:
    """``utxo`` holding exactly the minimum ADA of its own output."""
    output = TransactionOutput(
        Address.decode(utxo.address),
        utxo_value(utxo.lovelace, utxo.assets),
        datum=RawCBOR(bytes.fromhex(utxo.datum)) if utxo.datum else None,
    )
    return replace(utxo, lovelace=min_ada(output))


@dataclass
class Built:
    """An assembled unsigned transaction: its CBOR as built, and decoded."""

    cbor: str

    @property
    def tx(self) -> Transaction:
        """The decoded transaction (for inspection only; evaluate :attr:`cbor`)."""
        return Transaction.from_cbor(self.cbor)


def build(
    contribute: Callable[..., None],
    snapshot: object,
    *,
    slot: int,
) -> Built:
    """Forward-build ``snapshot`` into an unsigned transaction."""
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=slot))
    contribute(tx_builder, snapshot=snapshot)
    return Built(assemble_unsigned(tx_builder))


def redeemers(tx: Transaction) -> set[tuple[str, int, str]]:
    """``(purpose, index, CBOR hex)`` of every redeemer of ``tx``."""
    witness = tx.transaction_witness_set.redeemer
    items = (
        witness.items()
        if hasattr(witness, "items")
        else [(r.key, r.value) for r in witness]
    )
    out = set()
    for key, value in items:
        tag = key.tag.value if hasattr(key.tag, "value") else int(key.tag)
        out.add((_PURPOSE[int(tag)], int(key.index), value.data.to_cbor().hex()))
    return out


def captured_redeemers(fix: dict[str, Any]) -> set[tuple[str, int, str]]:
    """``(purpose, index, CBOR hex)`` of every redeemer of a capture."""
    return {(r["purpose"], r["index"], r["cbor"]) for r in fix["redeemers"]}


def output_view(
    output: Any,
) -> tuple[str, int, set[tuple[str, int]], str | None]:  # noqa: ANN401
    """``(address, lovelace, {(unit, qty)}, datum hex)`` of a built output."""
    assets = {
        (bytes(policy).hex() + name.payload.hex(), qty)
        for policy, names in output.amount.multi_asset.items()
        for name, qty in names.items()
    }
    datum = output.datum.to_cbor().hex() if output.datum is not None else None
    return str(output.address), output.amount.coin, assets, datum


def captured_output_view(output: dict[str, Any]) -> tuple[str, int, set, str | None]:
    """:func:`output_view` of a captured output."""
    assets = {(p + n, int(q)) for p, n, q in output["assets"]}
    return output["address"], int(output["lovelace"]), assets, output["datum"]


def evaluate(built: Built, utxos: Iterable[Utxo]) -> list[str]:
    """Ogmios-evaluate ``built`` with ``utxos`` supplied; the sorted redeemer purposes.

    Every script must succeed with a positive budget.
    """
    entries: dict[tuple[str, int] | None, dict[str, Any]] = {}
    for utxo in utxos:
        entries.setdefault(utxo.out_ref, ogmios_entry(utxo))
    budgets = evaluate_tx_cbor(built.cbor, list(entries.values()))
    for budget in budgets:
        assert budget["budget"]["memory"] > 0
        assert budget["budget"]["cpu"] > 0
    return sorted(b["validator"]["purpose"] for b in budgets)


def renamed(utxo: Utxo, out_ref: tuple[str, int], old: bytes, new: bytes) -> Utxo:
    """``utxo`` at ``out_ref`` with its pool or pool-manager NFT ``old`` renamed."""
    return replace(
        utxo,
        out_ref=out_ref,
        assets=[(p, new.hex() if n == old.hex() else n, q) for p, n, q in utxo.assets],
    )


def second_pool(
    position: PoolPosition,
    *,
    pool_ref: tuple[str, int],
    manager_ref: tuple[str, int],
) -> PoolPosition:
    """A copy of ``position`` at other out-refs, its NFTs renamed ``01`` + suffix.

    Ogmios reads it from ``additionalUtxo``, so a transaction can spend two pools
    of one owner although only one exists on chain.
    """
    old = position.pool_id
    new = b"\x01" + old[1:]
    assert old != new
    return PoolPosition(
        pool=renamed(position.pool, pool_ref, old, new),
        pool_manager=renamed(position.pool_manager, manager_ref, old, new),
    )


def pool_fixture_backend(
    monkeypatch: pytest.MonkeyPatch,
    fix: dict[str, Any],
    *,
    extra: Iterable[Utxo] = (),
) -> None:
    """Resolve pools, pool managers, the config and scripts from ``fix`` (+ ``extra``)."""
    from charli3_dendrite.lending.fluidtokens.transactions.utxos import (
        script_ref_by_hash,
    )
    from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve

    utxos = [utxo_from_dict(u) for u in fix["inputs"]] + list(extra)
    refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
    monkeypatch.setattr(
        resolve,
        "resolve_utxo",
        lambda _backend, out_ref, **_: next(u for u in utxos if u.out_ref == out_ref),
    )
    monkeypatch.setattr(
        resolve,
        "resolve_pool_manager_utxo",
        lambda _backend, pool_id, **_: next(
            u for u in utxos if u.holds(c.POOL_MANAGER_POLICY, pool_id.hex())
        ),
    )
    monkeypatch.setattr(
        resolve,
        "resolve_config_utxo",
        lambda _backend: next(
            u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
        ),
    )
    monkeypatch.setattr(
        resolve,
        "resolve_script",
        lambda _backend, h: script_ref_by_hash(
            refs,
            h.hex() if isinstance(h, bytes) else h,
        ),
    )


# The oracle NFT policy of FluidTokens' version-2 oracles, listed in the registry next
# to the version-1 oracles of the same tokens.
ORACLE_V2_POLICY = "26e60b2083c14b849e622f8e05dd46ab01a7986fe5d72eeba8680d26"
# The signer of the registry entries below (one key, as FluidTokens' multisig oracles).
_REGISTRY_SIGNER = "cb1506c82c3143948618c50834a527b8d471ebae067bc0a5dee1627dc511e914"


def registry_entry(
    witness: OracleWitness,
    oracle_token: str,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """A ``get-oracle-tokens`` registry entry serving ``witness``.

    ``oracle_token`` is the unit of the oracle NFT its feed holds, and ``token`` the
    unit it prices (by default the token of the witness's signed price). The entry
    carries the witness's signed price, window and signature; the reward the client
    rebuilds from it is the same message, not necessarily the same bytes.
    """
    reward = witness.reward
    priced = token or reward.collateral_policy + reward.collateral_name
    return {
        "token": {"policyId": priced[:56], "assetName": priced[56:]},
        "fluidOracle": {
            "policyId": oracle_token[:56],
            "assetName": oracle_token[56:],
            "referenceInput": "{}#{}".format(*witness.feed.out_ref),
            "referenceScript": "{}#{}".format(*witness.script_ref.out_ref),
        },
        "multisigOracle": {"publicKeys": [_REGISTRY_SIGNER], "requiredSignatures": 1},
        "preferredOracle": "multisig",
        "active": True,
        "supportedOracle": {
            "multisig": {
                "validFrom": reward.valid_from_ms,
                "validTo": reward.valid_to_ms,
                "tokenPriceInLovelaces": reward.price_num,
                "tokenPriceDenominator": reward.price_den,
                "multisigOracle": {
                    "signatures": [
                        {
                            "publicKey": _REGISTRY_SIGNER,
                            "signature": reward.signature.hex(),
                        },
                    ],
                    "requiredSignatures": 1,
                },
            },
        },
    }


def other_oracle(witness: OracleWitness, policy: str) -> tuple[OracleWitness, str]:
    """A stand-in second oracle for ``witness``'s token: its NFT under ``policy``.

    Returns the witness and its oracle NFT unit. The feed and script sit at other
    out-refs, as a second oracle version's do; the signed price is the same.
    """
    name = next(n for _, n, _ in witness.feed.assets)
    feed = replace(
        witness.feed,
        assets=[(policy, name, 1)],
        out_ref=(hashlib.blake2b(bytes.fromhex(policy + name)).hexdigest()[:64], 7),
    )
    script_ref = replace(
        witness.script_ref,
        ref_script=(b"stand-in oracle for " + bytes.fromhex(name)).hex(),
        out_ref=(feed.out_ref[0], 8),
    )
    return OracleWitness(feed, script_ref, witness.reward_cbor), policy + name


class RegistrySession:
    """A ``requests`` session answering every GET with ``entries``; never online."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self.calls: list[str] = []

    def get(self, url: str, **_: Any) -> Any:  # noqa: ANN401
        self.calls.append(url)
        entries = self._entries

        class _Response:
            status_code = 200
            text = ""

            def json(self) -> list[dict[str, Any]]:
                return entries

        return _Response()


def registry_backend(monkeypatch: pytest.MonkeyPatch, utxos: Iterable[Utxo]) -> None:
    """Resolve the registry's out-refs (feeds, oracle scripts) from ``utxos``."""
    from charli3_dendrite.lending.fluidtokens.transactions import resolve

    by_ref = {u.out_ref: u for u in utxos}
    monkeypatch.setattr(
        resolve,
        "resolve_utxo_by_outref",
        lambda _backend, tx_hash, index, **_: by_ref[(tx_hash, index)],
    )


def utxo_record(utxo: Utxo) -> Any:  # noqa: ANN401
    """The backend record of ``utxo``, as the V4 indexer reads it."""
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.dataclasses.models import PoolStateInfo

    tx_hash, index = utxo.out_ref or ("", 0)
    return PoolStateInfo(
        address=utxo.address,
        tx_hash=tx_hash,
        tx_index=index,
        block_time=0,
        block_index=0,
        block_hash="",
        datum_hash="",
        datum_cbor=utxo.datum or "",
        assets=Assets(
            root={"lovelace": utxo.lovelace} | {p + n: q for p, n, q in utxo.assets},
        ),
        plutus_v2=False,
    )


def claim_fixture_backend(
    monkeypatch: pytest.MonkeyPatch,
    fix: dict[str, Any],
    *,
    extra: Iterable[Utxo] = (),
) -> None:
    """Resolve a claim from ``fix`` (+ ``extra``): configs, scripts, UTxOs, the index."""
    from charli3_dendrite.lending.fluidtokens_v4.indexing import parse_utxo
    from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve

    pool_fixture_backend(monkeypatch, fix, extra=extra)
    utxos = [utxo_from_dict(u) for u in fix["inputs"]] + list(extra)
    refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
    monkeypatch.setattr(
        resolve,
        "resolve_lender_manager_config_utxo",
        lambda _backend: next(
            u
            for u in refs
            if u.holds(
                c.LENDER_MANAGER_CONFIG_NFT_POLICY,
                c.LENDER_MANAGER_CONFIG_NFT_NAME,
            )
        ),
    )
    monkeypatch.setattr(
        resolve,
        "fetch_entities",
        lambda _backend, selector: [
            state
            for state in (
                parse_utxo(utxo_record(u), {selector.kind: selector}) for u in utxos
            )
            if state is not None
        ],
    )
