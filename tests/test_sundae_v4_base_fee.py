"""SundaeV4Vault.base_fee(): the live fee-settings read and its manifest fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dexs.amm import sundae_v4
from charli3_dendrite.dexs.amm.sundae_v4 import FeeSettings
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4ConstantSumPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from tests.sundae_v4_vault_factory import build_vault_utxo
from tests.test_sundae_v4_backend_redeemers import _Minimal

_FIX = json.loads(
    (Path(__file__).parent / "sundae_v4_mainnet_fixtures.json").read_text(),
)
_ADDRESS = Address(
    payment_part=VerificationKeyHash(bytes.fromhex("aa" * 28)),
    staking_part=VerificationKeyHash(bytes.fromhex("bb" * 28)),
    network=Network.MAINNET,
)


@pytest.fixture(autouse=True)
def _isolate() -> None:
    """Point the class family at mainnet and clear the base-fee cache per test."""
    SundaeV4Vault.select_network("mainnet")
    SundaeV4Vault.clear_base_fee_cache()
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_base_fee_cache()


class _FeeSettingsBackend(_Minimal):
    """Serves a fixed fee-settings datum (or a failure) and records the query."""

    def __init__(
        self,
        datum_cbor: str | None,
        *,
        raises: type[Exception] | None = None,
    ) -> None:
        """Store the datum (or exception type) to serve and reset the call log."""
        self.datum_cbor = datum_cbor
        self.raises = raises
        self.calls: list[tuple[Address, str | None]] = []

    def get_datum_from_address(
        self,
        address: Address,
        asset: str | None = None,
    ) -> ScriptReference | None:
        """Record the call and either raise, return ``None``, or serve the datum."""
        self.calls.append((address, asset))
        if self.raises is not None:
            raise self.raises("simulated backend failure")
        if self.datum_cbor is None:
            return None
        return ScriptReference(
            tx_hash=None,
            tx_index=None,
            address=None,
            assets=None,
            datum_hash=None,
            datum_cbor=self.datum_cbor,
            script=None,
        )


def _mainnet_pool() -> SundaeV4ConstantSumPool:
    """A two-asset mainnet-shaped constant-sum pool for base-fee tests."""
    values, config = build_vault_utxo(
        [("aa" * 28 + "01", 1_000), ("bb" * 28 + "02", 1_000)],
        prices=[1, 1],
        total_lp=2_000,
        network="mainnet",
    )
    vault = SundaeV4Vault.model_validate(values)
    cs = SundaeV4Deployment.for_network("mainnet").validator("constant_sum.withdraw")
    vault.supply_module_config(cs, config)
    return vault.pools()[0]


def test_base_fee_reads_the_live_fee_settings_node_and_flows_into_the_builders() -> (
    None
):
    deployment = SundaeV4Deployment.for_network("mainnet")
    backend = _FeeSettingsBackend(FeeSettings(base_fee=1_500_000).to_cbor_hex())
    set_backend(backend)

    assert SundaeV4Vault.base_fee() == 1_500_000
    assert backend.calls == [
        (deployment.settings_address, deployment.fee_settings_unit)
    ]

    pool = _mainnet_pool()
    built = pool.swap_datum(
        address_source=_ADDRESS,
        in_assets=Assets(**{"aa" * 28 + "01": 100}),
        out_assets=Assets(**{"bb" * 28 + "02": 90}),
    )
    assert built.service_budget == built.max_per_execution == 1_500_000
    assert pool.batcher_fee() == Assets(lovelace=1_500_000)

    output, _datum = pool.swap_utxo(
        address_source=_ADDRESS,
        in_assets=Assets(**{"aa" * 28 + "01": 100}),
        out_assets=Assets(**{"bb" * 28 + "02": 90}),
    )
    assert output.amount.coin == 1_500_000 + 2_000_000


def test_base_fee_reads_the_recorded_mainnet_fee_settings_datum() -> None:
    entry = next(e for e in _FIX["settings"] if e["label"] == "fee-settings")
    set_backend(_FeeSettingsBackend(entry["datum"]))
    assert SundaeV4Vault.base_fee() == 1_280_000


def test_base_fee_is_cached_within_the_ttl() -> None:
    backend = _FeeSettingsBackend(FeeSettings(base_fee=1_500_000).to_cbor_hex())
    set_backend(backend)

    assert SundaeV4Vault.base_fee() == 1_500_000
    assert SundaeV4Vault.base_fee() == 1_500_000
    assert len(backend.calls) == 1


def test_base_fee_refetches_after_the_ttl_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FeeSettingsBackend(FeeSettings(base_fee=1_500_000).to_cbor_hex())
    set_backend(backend)

    clock = {"t": 1_000.0}
    monkeypatch.setattr(sundae_v4.time, "monotonic", lambda: clock["t"])

    assert SundaeV4Vault.base_fee() == 1_500_000
    clock["t"] += sundae_v4._BASE_FEE_TTL_S + 1
    assert SundaeV4Vault.base_fee() == 1_500_000
    assert len(backend.calls) == 2


def test_base_fee_falls_back_to_the_manifest_when_the_backend_is_unimplemented() -> (
    None
):
    manifest = SundaeV4Deployment.for_network("mainnet").base_fee
    set_backend(_FeeSettingsBackend(None, raises=NotImplementedError))
    assert SundaeV4Vault.base_fee() == manifest


def test_base_fee_falls_back_to_the_manifest_on_a_missing_datum() -> None:
    manifest = SundaeV4Deployment.for_network("mainnet").base_fee
    set_backend(_FeeSettingsBackend(None))
    assert SundaeV4Vault.base_fee() == manifest


def test_base_fee_falls_back_to_the_manifest_when_no_backend_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = SundaeV4Deployment.for_network("mainnet").base_fee

    def _raise() -> None:
        raise ValueError("Backend has not been set. Call set_backend() first.")

    monkeypatch.setattr(sundae_v4, "get_backend", _raise)
    assert SundaeV4Vault.base_fee() == manifest


def test_base_fee_falls_back_to_the_manifest_on_an_undecodable_datum() -> None:
    manifest = SundaeV4Deployment.for_network("mainnet").base_fee
    set_backend(_FeeSettingsBackend("ff"))
    assert SundaeV4Vault.base_fee() == manifest


def test_base_fee_falls_back_value_is_also_cached_within_the_ttl() -> None:
    manifest = SundaeV4Deployment.for_network("mainnet").base_fee
    backend = _FeeSettingsBackend(None, raises=NotImplementedError)
    set_backend(backend)

    assert SundaeV4Vault.base_fee() == manifest
    assert SundaeV4Vault.base_fee() == manifest
    assert len(backend.calls) == 1
