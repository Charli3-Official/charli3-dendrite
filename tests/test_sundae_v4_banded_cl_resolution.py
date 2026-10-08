"""Config resolution for banded CL vaults, including the two preview traps.

The four preview banded pools were created under the superseded module build
(``0977898c…``) with the pre-index three-field config, then moved by a governance
upgrade to the current build (``33485bba…``), which derived the ladder index on
chain and re-committed the four-field config. So the upgraded vault's producing
transaction carries no banded redeemer, the Create redeemer at the bottom of its
history belongs to a different script hash than the one its ``module_state`` is
keyed by, and no redeemer anywhere holds the committed config. Resolution must
still find the config: by accepting every build of the kind, parsing the old shape
and rebuilding the index, then verifying by hash.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import RedeemerRecord
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLConfig
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLConfigV0
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLCreateV0
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLEntry
from charli3_dendrite.dexs.amm.sundae_v4 import BandedCLOperate
from charli3_dendrite.dexs.amm.sundae_v4 import OutputReference
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4BandedCLPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.amm.sundae_v4 import parse_banded_cl_config
from charli3_dendrite.dexs.core.errors import ModuleConfigUnavailableError
from tests.sundae_v4_vault_factory import build_banded_cl_vault_utxo
from tests.test_sundae_v4_backend_redeemers import _Minimal
from tests.test_sundae_v4_banded_cl_math import P1_CLOSING
from tests.test_sundae_v4_banded_cl_math import P1_RESERVES
from tests.test_sundae_v4_banded_cl_math import P1_STARTS
from tests.test_sundae_v4_banded_cl_pool import P1_COMMITMENT
from tests.test_sundae_v4_banded_cl_pool import P1_TOTAL_LP
from tests.test_sundae_v4_banded_cl_pool import TOKENA
from tests.test_sundae_v4_banded_cl_pool import TOKENC

V0_COMMITMENT = "10cd2c7139c5f626d50e504d64e7ca95e5a05d0a734640678e3c005ea78aa15f"
CREATE_TX = "c6" * 32
EXECUTE_TX = "e2" * 32


class _History(_Minimal):
    """Serves recorded redeemers by transaction and the pool NFT's recorded states."""

    def __init__(
        self, redeemers: dict[str, list[RedeemerRecord]], states: list[dict]
    ) -> None:
        self.redeemers = redeemers
        self.states = states
        self.redeemer_calls: list[str] = []

    def get_redeemers(self, tx_hash: str) -> list[RedeemerRecord]:
        self.redeemer_calls.append(tx_hash)
        return self.redeemers.get(tx_hash, [])

    def get_pool_utxos(self, *args: object, **kwargs: object) -> PoolStateList:
        address = SundaeV4Deployment.for_network("preview").pool_address.encode()
        return PoolStateList(
            root=[
                PoolStateInfo(
                    address=address,
                    tx_hash=s["tx"],
                    tx_index=0,
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


def _states() -> tuple[dict, dict, BandedCLConfig, BandedCLConfigV0]:
    """The vault at creation (old build, old config) and after the upgrade."""
    created, config = build_banded_cl_vault_utxo(
        [(TOKENA, P1_RESERVES[0]), (TOKENC, P1_RESERVES[1])],
        starts=P1_STARTS,
        closing=P1_CLOSING,
        total_lp=P1_TOTAL_LP,
        module_title="banded_cl.withdraw.superseded1",
        legacy=True,
    )
    legacy = BandedCLConfigV0(
        bands=config.bands, closing=config.closing, weight_total=config.weight_total
    )
    assert module_config_hash(legacy).hex() == V0_COMMITMENT
    created["tx_hash"] = CREATE_TX
    upgraded, same = build_banded_cl_vault_utxo(
        [(TOKENA, P1_RESERVES[0]), (TOKENC, P1_RESERVES[1])],
        starts=P1_STARTS,
        closing=P1_CLOSING,
        total_lp=P1_TOTAL_LP,
    )
    assert module_config_hash(same).hex() == P1_COMMITMENT
    upgraded["tx_hash"] = EXECUTE_TX
    return created, upgraded, config, legacy


def _create_record(legacy: BandedCLConfigV0) -> RedeemerRecord:
    create = BandedCLCreateV0(initial_state=legacy, pool_output_index=0, initial_band=3)
    return RedeemerRecord(
        tx_hash=CREATE_TX,
        purpose="reward",
        index=0,
        script_hash=_deployment().validator("banded_cl.withdraw.superseded1").hex(),
        data_cbor=create.to_cbor_hex(),
    )


def _vault(values: dict) -> SundaeV4Vault:
    return SundaeV4Vault.model_validate(dict(values))


def test_parse_accepts_both_shapes_and_indexes_the_old_one() -> None:
    _created, _upgraded, config, legacy = _states()
    assert (
        module_config_hash(parse_banded_cl_config(legacy.to_cbor())).hex()
        == P1_COMMITMENT
    )
    assert (
        module_config_hash(parse_banded_cl_config(config.to_cbor())).hex()
        == P1_COMMITMENT
    )
    assert isinstance(parse_banded_cl_config(legacy), BandedCLConfig)


def test_the_upgraded_vault_resolves_from_the_old_builds_create_redeemer() -> None:
    created, upgraded, _config, legacy = _states()
    backend = _History(
        redeemers={CREATE_TX: [_create_record(legacy)], EXECUTE_TX: []},
        states=[
            {
                "tx": CREATE_TX,
                "block_time": 1,
                "datum": created["datum_cbor"],
                "value": created["assets"],
            },
            {
                "tx": EXECUTE_TX,
                "block_time": 2,
                "datum": upgraded["datum_cbor"],
                "value": upgraded["assets"],
            },
        ],
    )
    set_backend(backend)
    vault = _vault(upgraded)
    module = _deployment().banded_cl_hash
    assert vault.module_kind(module) == "banded_cl"
    config = vault.module_config(module)
    assert isinstance(config, BandedCLConfig)
    assert module_config_hash(config).hex() == P1_COMMITMENT
    # The producing (Execute) transaction was read first, then the history.
    assert backend.redeemer_calls == [EXECUTE_TX, CREATE_TX]
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4BandedCLPool)
    assert pool.get_amount_out(Assets(**{TOKENA: 1_000}), TOKENC)[0].quantity() == 996


def test_the_created_vault_resolves_its_own_old_config() -> None:
    created, _upgraded, _config, legacy = _states()
    set_backend(_History(redeemers={CREATE_TX: [_create_record(legacy)]}, states=[]))
    vault = _vault(created)
    old = _deployment().validator("banded_cl.withdraw.superseded1")
    assert vault.module_state[old].hex() == V0_COMMITMENT
    # The old commitment is the hash of the three-field shape, which the parser
    # always re-indexes, so the committed preimage is not recoverable as a
    # four-field config: the old build's pools are only priceable after the
    # upgrade, which is the state every preview pool is in.
    with pytest.raises(ModuleConfigUnavailableError):
        vault.module_config(old)


def test_a_scoop_resolves_from_its_own_operate_entry() -> None:
    _created, upgraded, config, _legacy = _states()
    scoop_tx = "5c" * 32
    upgraded["tx_hash"] = scoop_tx
    entry = BandedCLEntry(
        pool_oref=OutputReference(
            transaction_id=bytes.fromhex(EXECUTE_TX), output_index=0
        ),
        config=config,
        counter=1_000_049_919,
        active_band=3,
    )
    record = RedeemerRecord(
        tx_hash=scoop_tx,
        purpose="reward",
        index=0,
        script_hash=_deployment().banded_cl_hash.hex(),
        data_cbor=BandedCLOperate(entries=[entry]).to_cbor_hex(),
    )
    backend = _History(redeemers={scoop_tx: [record]}, states=[])
    set_backend(backend)
    vault = _vault(upgraded)
    resolved = vault.module_config(_deployment().banded_cl_hash)
    assert module_config_hash(resolved).hex() == P1_COMMITMENT
    assert backend.redeemer_calls == [scoop_tx]
    assert BandedCLOperate.from_cbor(record.data_cbor).to_cbor_hex() == record.data_cbor


def test_a_wrong_ladder_in_the_history_is_not_accepted() -> None:
    created, upgraded, _config, legacy = _states()
    wrong = BandedCLConfigV0(
        bands=list(legacy.bands)[:7],
        closing=legacy.closing,
        weight_total=7,
    )
    set_backend(
        _History(
            redeemers={CREATE_TX: [_create_record(wrong)], EXECUTE_TX: []},
            states=[
                {
                    "tx": CREATE_TX,
                    "block_time": 1,
                    "datum": created["datum_cbor"],
                    "value": created["assets"],
                },
            ],
        )
    )
    vault = _vault(upgraded)
    with pytest.raises(ModuleConfigUnavailableError):
        vault.module_config(_deployment().banded_cl_hash)
