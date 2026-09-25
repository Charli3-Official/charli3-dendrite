"""FluidTokens V4 constants and config-datum resolution (offline)."""

import json
from pathlib import Path

from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.units import constr

FIX = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())


def test_default_config_equals_the_live_config_datum_byte_exact():
    # The constants must rebuild the newest captured config version exactly, which
    # pins every constant to its datum field.
    assert c.default_config().to_cbor_hex() == FIX["config"][-1]["datum_cbor"]


def test_config_history_changed_only_action_script_hashes():
    versions = [ConfigDatum.from_cbor(rec["datum_cbor"]) for rec in FIX["config"]]
    changed = set()
    for older, newer in zip(versions, versions[1:]):
        changed |= {
            name for name, value in vars(newer).items() if getattr(older, name) != value
        }
    assert changed
    assert all(name.endswith("_action_script_hash") for name in changed)


def test_config_fixtures_hold_the_config_nft():
    unit = c.CONFIG_NFT_POLICY + c.CONFIG_NFT_NAME
    assert all(rec["assets"].get(unit) == 1 for rec in FIX["config"])


def test_lender_manager_config_fixture_holds_its_nft():
    unit = c.LENDER_MANAGER_CONFIG_NFT_POLICY + c.LENDER_MANAGER_CONFIG_NFT_NAME
    assert FIX["lender_manager_config"]["assets"].get(unit) == 1


class _NoDbsyncBackend:
    """A backend without ``db_query``: the V3 resolver raises ``TypeError``."""


class _Utxo:
    def __init__(self, datum):
        self.datum = datum


def test_resolve_config_reads_the_live_datum(monkeypatch):
    from charli3_dendrite.lending.fluidtokens.transactions import resolve

    live = ConfigDatum.from_cbor(FIX["config"][0]["datum_cbor"])
    monkeypatch.setattr(
        resolve,
        "resolve_utxo_by_asset",
        lambda backend, policy, name: _Utxo(FIX["config"][0]["datum_cbor"]),
    )
    assert c.resolve_config(object()) == live
    assert c.resolve_config(object()) != c.default_config()


def test_resolve_config_falls_back_without_dbsync():
    assert c.resolve_config(_NoDbsyncBackend()) == c.default_config()


def test_resolve_config_falls_back_when_no_utxo_holds_the_nft(monkeypatch):
    from charli3_dendrite.lending.fluidtokens.transactions import resolve

    def missing(backend, policy, name):
        raise ValueError(f"no UTxO holds {policy}{name}")

    monkeypatch.setattr(resolve, "resolve_utxo_by_asset", missing)
    assert c.resolve_config(object()) == c.default_config()


def test_resolve_config_falls_back_on_an_undecodable_datum(monkeypatch):
    from charli3_dendrite.lending.fluidtokens.transactions import resolve

    # A V3-shaped config datum (22 fields) must not decode as the V4 ConfigDatum.
    monkeypatch.setattr(
        resolve,
        "resolve_utxo_by_asset",
        lambda backend, policy, name: _Utxo(
            "d8799f" + "40" * 22 + "ff",
        ),
    )
    assert c.resolve_config(object()) == c.default_config()


def test_resolve_config_falls_back_when_the_utxo_has_no_datum(monkeypatch):
    from charli3_dendrite.lending.fluidtokens.transactions import resolve

    monkeypatch.setattr(
        resolve,
        "resolve_utxo_by_asset",
        lambda backend, policy, name: _Utxo(None),
    )
    assert c.resolve_config(object()) == c.default_config()


def test_resolve_config_falls_back_on_truncated_cbor(monkeypatch):
    from charli3_dendrite.lending.fluidtokens.transactions import resolve

    # Truncated CBOR raises cbor2's end-of-stream error, which is not a ValueError.
    monkeypatch.setattr(
        resolve,
        "resolve_utxo_by_asset",
        lambda backend, policy, name: _Utxo("d87a"),
    )
    assert c.resolve_config(object()) == c.default_config()


def test_lender_manager_spend_hash_receives_live_lender_bonds():
    # The config datum does not list the lender manager; the captured pools name its
    # spend script as the payment credential of their lender bond address.
    script_hashes = set()
    for rec in FIX["pool"]:
        address = PoolDatum.from_cbor(rec["datum_cbor"]).lender_bond_address
        _, (payment, _staking) = constr(address)
        payment_alt, (payment_hash,) = constr(payment)
        if payment_alt == 1:  # ScriptCredential
            script_hashes.add(payment_hash.hex())
    assert c.LENDER_MANAGER_SPEND_SKH in script_hashes
