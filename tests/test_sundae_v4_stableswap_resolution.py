"""Config resolution for stableswap vaults, including states a rate update produced.

A rate-update scoop commits ``hash(config with the new rates)`` in the vault it
produces, while its Operate entry carries the old config; the new rates exist only in
the scoop's transcript. Every recorded mainnet state must resolve, hash-verified,
from its own transaction or the pool NFT's history.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import RedeemerRecord
from charli3_dendrite.dexs.amm import sundae_v4
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapConfig
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.core.errors import ModuleConfigUnavailableError
from tests.test_sundae_v4_backend_redeemers import _Minimal
from tests.test_sundae_v4_stableswap_types import MAINNET
from tests.test_sundae_v4_stableswap_types import TESTNET


class _History(_Minimal):
    """Serves recorded redeemers by transaction and the vault NFT's recorded states."""

    def __init__(
        self, redeemers: dict[str, list[dict]], states: list[dict], address: str
    ) -> None:
        self.redeemers = {
            tx: [RedeemerRecord(**r) for r in rs] for tx, rs in redeemers.items()
        }
        self.states = states
        self.address = address

    def get_redeemers(self, tx_hash: str) -> list[RedeemerRecord]:
        return self.redeemers.get(tx_hash, [])

    def get_pool_utxos(self, *args, **kwargs) -> PoolStateList:
        return PoolStateList(
            root=[
                PoolStateInfo(
                    address=self.address,
                    tx_hash=s["tx"],
                    tx_index=s["index"],
                    block_time=s["block_time"],
                    block_index=0,
                    block_hash="00" * 32,
                    datum_hash="11" * 32,
                    datum_cbor=s["datum"],
                    assets=Assets(**s["value"]),
                    plutus_v2=False,
                )
                for s in self.states
            ]
        )


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    SundaeV4Vault.select_network("mainnet")
    SundaeV4Vault.clear_config_cache()
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_config_cache()


def _vault(state: dict) -> SundaeV4Vault:
    return SundaeV4Vault.model_validate(
        {
            "tx_hash": state["tx"],
            "tx_index": state["index"],
            "datum_cbor": state["datum"],
            "assets": state["value"],
            "block_time": state["block_time"],
        }
    )


def _mainnet_backend(upto: int) -> _History:
    states = MAINNET["pool_states"][: upto + 1]
    address = SundaeV4Deployment.for_network("mainnet").pool_address.encode()
    return _History(MAINNET["redeemers"], states, address)


@pytest.mark.parametrize("index", range(11))
def test_every_recorded_mainnet_state_resolves_its_config(index: int) -> None:
    set_backend(_mainnet_backend(index))
    state = MAINNET["pool_states"][index]
    vault = _vault(state)
    module = bytes.fromhex(MAINNET["module"])
    config = vault.module_config(module)
    assert isinstance(config, StableSwapConfig)
    assert module_config_hash(config) == vault.module_state[module]
    expected = [1_000_000, 1_000_001] if index == 2 else [1_000_000, 1_000_000]
    assert [int(r) for r in config.rates] == expected


def test_the_rate_update_state_needs_the_transcript_rates(monkeypatch) -> None:
    set_backend(_mainnet_backend(2))
    monkeypatch.setattr(sundae_v4, "_rate_update_rates", lambda records, pool_hash: [])
    with pytest.raises(ModuleConfigUnavailableError):
        _vault(MAINNET["pool_states"][2]).module_config(
            bytes.fromhex(MAINNET["module"])
        )


def test_the_preview_rate_update_state_resolves() -> None:
    SundaeV4Vault.select_network("preview")
    (tx,) = [t for t in TESTNET["transactions"] if t["tx"].startswith("0a6b971c")]
    (state,) = tx["pool_states"]
    address = SundaeV4Deployment.for_network("preview").pool_address.encode()
    set_backend(_History({tx["tx"]: tx["redeemers"]}, [state], address))
    vault = _vault(state)
    module = SundaeV4Deployment.for_network("preview").stableswap_hash
    config = vault.module_config(module)
    assert [int(r) for r in config.rates] == [1_000_000, 1_001_000]
