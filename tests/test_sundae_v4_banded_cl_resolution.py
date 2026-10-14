"""Config resolution and binding for banded CL vaults.

A banded vault's ``module_state`` commits to a ladder the datum does not carry. It
is recovered from the module's ``Create`` / ``Operate`` redeemers as the four-field
:class:`BandedCLConfig` and hash-verified, like every other module config. A vault
bound to the superseded (pre-index) build of the module is not priced, and neither
is one whose trade action also binds the oracle module.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pycardano import IndefiniteList
from pycardano import RawPlutusData
from pycardano.serialization import CBORTag

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLConfig
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLCreate
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLEntry
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLOperate
from charli3_dendrite.dexs.amm.sundae_v4 import OutputReference
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4BandedCLPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.dexs.core.errors import ModuleConfigUnavailableError
from charli3_dendrite.dexs.core.errors import NotAPoolError

from tests.sundae_v4_vault_factory import build_banded_cl_vault_utxo
from tests.test_sundae_v4_backend_redeemers import _Minimal
from tests.test_sundae_v4_banded_cl_math import P1_CLOSING
from tests.test_sundae_v4_banded_cl_math import P1_RESERVES
from tests.test_sundae_v4_banded_cl_math import P1_STARTS
from tests.test_sundae_v4_banded_cl_pool import P1_COMMITMENT
from tests.test_sundae_v4_banded_cl_pool import P1_TOTAL_LP
from tests.test_sundae_v4_banded_cl_pool import TOKENA
from tests.test_sundae_v4_banded_cl_pool import TOKENC
from tests.test_sundae_v4_stableswap_resolution import _History

CREATE_TX = "c6" * 32
SCOOP_TX = "5c" * 32
EXECUTE_TX = "e2" * 32
SUPERSEDED = "banded_concentrated_liquidity.withdraw.superseded1"
VAULTS = json.loads(
    (Path(__file__).parent / "sundae_v4_banded_cl_vaults.json").read_text()
)
# oracle.withdraw on preview, as the Sundae API lists it; not in the manifest.
ORACLE_MODULE = bytes.fromhex(
    "144f4c996e3e85a10f2c3bd3d01b32fc4bdee1d237167fc7dd678d59"
)


@pytest.fixture(autouse=True)
def _preview() -> Iterator[None]:
    SundaeV4Vault.select_network("preview")
    SundaeV4Vault.clear_config_cache()
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_config_cache()


def _deployment() -> SundaeV4Deployment:
    return SundaeV4Deployment.for_network("preview")


def _p1(**kwargs: object) -> tuple[dict, BandedCLConfig]:
    return build_banded_cl_vault_utxo(
        [(TOKENA, P1_RESERVES[0]), (TOKENC, P1_RESERVES[1])],
        starts=P1_STARTS,
        closing=P1_CLOSING,
        total_lp=P1_TOTAL_LP,
        **kwargs,  # type: ignore[arg-type]
    )


def _record(tx_hash: str, redeemer: RawPlutusData | BandedCLCreate) -> dict:
    return {
        "tx_hash": tx_hash,
        "purpose": "reward",
        "index": 0,
        "script_hash": _deployment().banded_cl_hash.hex(),
        "data_cbor": redeemer.to_cbor_hex(),
    }


def _history(redeemers: dict[str, list[dict]], states: list[dict]) -> _History:
    return _History(redeemers, states, _deployment().pool_address.encode())


def _state(tx_hash: str, block_time: int, values: dict) -> dict:
    return {
        "tx": tx_hash,
        "index": 0,
        "block_time": block_time,
        "datum": values["datum_cbor"],
        "value": values["assets"],
    }


def test_a_scoop_resolves_from_its_own_operate_entry() -> None:
    values, config = _p1()
    values["tx_hash"] = SCOOP_TX
    entry = BandedCLEntry(
        pool_oref=OutputReference(
            transaction_id=bytes.fromhex(EXECUTE_TX), output_index=0
        ),
        config=config,
        counter=1_000_049_919,
        active_band=3,
    )
    operate = BandedCLOperate(entries=[entry])
    set_backend(_history({SCOOP_TX: [_record(SCOOP_TX, operate)]}, []))
    vault = SundaeV4Vault.model_validate(values)
    resolved = vault.module_config(_deployment().banded_cl_hash)
    assert isinstance(resolved, BandedCLConfig)
    assert module_config_hash(resolved).hex() == P1_COMMITMENT
    assert BandedCLOperate.from_cbor(operate.to_cbor_hex()).to_cbor_hex() == (
        operate.to_cbor_hex()
    )
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4BandedCLPool)
    assert pool.get_amount_out(Assets(**{TOKENA: 1_000}), TOKENC)[0].quantity() == 996


def test_the_create_redeemer_at_the_bottom_of_the_history_resolves_it() -> None:
    created, config = _p1()
    created["tx_hash"] = CREATE_TX
    current, _ = _p1()
    current["tx_hash"] = EXECUTE_TX
    create = BandedCLCreate(initial_state=config, pool_output_index=0, initial_band=3)
    set_backend(
        _history(
            {CREATE_TX: [_record(CREATE_TX, create)], EXECUTE_TX: []},
            [_state(CREATE_TX, 1, created), _state(EXECUTE_TX, 2, current)],
        )
    )
    vault = SundaeV4Vault.model_validate(current)
    resolved = vault.module_config(_deployment().banded_cl_hash)
    assert module_config_hash(resolved).hex() == P1_COMMITMENT


def test_a_wrong_ladder_in_the_history_is_not_accepted() -> None:
    created, config = _p1()
    created["tx_hash"] = CREATE_TX
    _, wrong = build_banded_cl_vault_utxo(
        [(TOKENA, P1_RESERVES[0]), (TOKENC, P1_RESERVES[1])],
        starts=P1_STARTS[:7],
        closing=P1_STARTS[7],
        total_lp=P1_TOTAL_LP,
    )
    create = BandedCLCreate(initial_state=wrong, pool_output_index=0, initial_band=3)
    set_backend(
        _history(
            {CREATE_TX: [_record(CREATE_TX, create)]},
            [_state(CREATE_TX, 1, created)],
        )
    )
    vault = SundaeV4Vault.model_validate({**created, "tx_hash": EXECUTE_TX})
    with pytest.raises(ModuleConfigUnavailableError):
        vault.module_config(_deployment().banded_cl_hash)


def test_a_vault_bound_to_the_superseded_build_fails_closed() -> None:
    values, config = _p1(module_title=SUPERSEDED)
    vault = SundaeV4Vault.model_validate(values)
    old = _deployment().validator(SUPERSEDED)
    assert vault.invariant_modules() == [(100, old, "banded_cl")]
    # Refused before any config is resolved: the backend is never read.
    set_backend(_Minimal())
    with pytest.raises(InvalidPoolError, match="superseded build"):
        vault.pools()
    # A supplied config does not change that.
    vault.supply_module_config(old, config)
    with pytest.raises(InvalidPoolError, match="superseded build"):
        vault.pools()
    with pytest.raises(InvalidPoolError, match="superseded build"):
        SundaeV4BandedCLPool.from_vault(vault, 100, config)


def test_a_pre_index_config_is_rejected_not_dereferenced() -> None:
    values, config = _p1()
    vault = SundaeV4Vault.model_validate(values)
    pre_index = RawPlutusData(
        CBORTag(121, [IndefiniteList(list(config.bands)), config.closing, 8])
    )
    with pytest.raises(InvalidPoolError, match="not a BandedCLConfig"):
        SundaeV4BandedCLPool.from_vault(vault, 100, pre_index)


@pytest.mark.parametrize("network", ["preview", "preprod"])
def test_a_recorded_oracle_enabled_vault_is_not_a_pool(network: str) -> None:
    SundaeV4Vault.select_network(network)
    utxo = VAULTS["oracle_pools"][network]
    with pytest.raises(NotAPoolError):
        SundaeV4Vault.model_validate({**utxo, "block_time": 0, "block_index": 0})


def test_a_trade_action_binding_a_module_the_manifest_lacks_fails_closed() -> None:
    values, config = _p1(extra_trade_modules=(ORACLE_MODULE,))
    vault = SundaeV4Vault.model_validate(values)
    assert vault.module_kind(ORACLE_MODULE) is None
    vault.supply_module_config(_deployment().banded_cl_hash, config)
    with pytest.raises(InvalidPoolError, match="does not know"):
        vault.pools()
