"""Live-fixture validation of the mainnet SundaeSwap V4 deployment.

The fixture material (``sundae_v4_mainnet_fixtures.json``) is real inline-datum
and redeemer CBOR captured from the launch pool via db-sync: its creation and
latest states, the creation transaction's module ``Create`` redeemers, the
latest state's (treasury-action) redeemers, every settings-node datum, and the
recorded orders.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.dataclasses.models import RedeemerRecord
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationFixed
from charli3_dendrite.dexs.amm.sundae_v4 import FeeSettings
from charli3_dendrite.dexs.amm.sundae_v4 import OrderConfig
from charli3_dendrite.dexs.amm.sundae_v4 import PoolConfig
from charli3_dendrite.dexs.amm.sundae_v4 import SettingsDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash
from charli3_dendrite.dexs.amm.sundae_v4 import parse_basic_constraint
from charli3_dendrite.dexs.core.errors import ModuleConfigUnavailableError
from tests.test_sundae_v4_backend_redeemers import _Minimal

_FIX = json.loads(
    (Path(__file__).parent / "sundae_v4_mainnet_fixtures.json").read_text()
)
_DEPLOYMENT = SundaeV4Deployment.for_network("mainnet")
_CS = _DEPLOYMENT.validator("constant_sum.withdraw")
_CREATION, _LATEST = _FIX["pool_states"]

_SETTINGS_CLASSES: dict[str, type] = {
    "settings": SettingsDatum,
    "fee-settings": FeeSettings,
    "cs-pool": PoolConfig,
    "basic-order": OrderConfig,
    "strategy-order": OrderConfig,
}


@pytest.fixture(autouse=True)
def _mainnet() -> Iterator[None]:
    """Point the class family at mainnet for each test, restoring it after."""
    SundaeV4Vault.select_network("mainnet")
    SundaeV4Vault.clear_config_cache()
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_config_cache()


def _vault_values(rec: dict) -> dict:
    """The ``SundaeV4Vault.model_validate`` input for a recorded pool state."""
    return {
        "tx_hash": rec["tx"],
        "tx_index": rec["index"],
        "datum_cbor": rec["datum"],
        "assets": rec["value"],
        "block_time": rec["block_time"],
    }


def _records(raw: list[dict]) -> list[RedeemerRecord]:
    """``RedeemerRecord`` objects from the fixture's recorded redeemer dicts."""
    return [RedeemerRecord(**r) for r in raw]


def _pool_state_info(rec: dict) -> PoolStateInfo:
    """A ``PoolStateInfo`` for the vault address from a recorded pool state."""
    return PoolStateInfo(
        address=_DEPLOYMENT.pool_address.encode(),
        tx_hash=rec["tx"],
        tx_index=rec["index"],
        block_time=rec["block_time"],
        block_index=0,
        block_hash="00" * 32,
        datum_hash="11" * 32,
        datum_cbor=rec["datum"],
        assets=Assets(**rec["value"]),
        plutus_v2=False,
    )


def _assets_from_pairs(pairs: list) -> Assets:
    """Dendrite ``Assets`` from a decoded ``[AssetClass, amount]`` pair list."""
    out: dict[str, int] = {}
    for entry in pairs:
        fields = list(entry)
        asset_class = AssetClass.from_primitive(fields[0])
        unit = (asset_class.policy.hex() + asset_class.asset_name.hex()) or "lovelace"
        out[unit] = int(fields[1])
    return Assets(**out)


class _WalkBackend(_Minimal):
    """Serves recorded redeemers by tx hash and the pool NFT's historical states."""

    def __init__(
        self,
        redeemers: dict[str, list[RedeemerRecord]],
        states: list[PoolStateInfo],
    ) -> None:
        """Store the redeemer table and historical states this backend serves."""
        self.redeemers = redeemers
        self.states = states
        self.calls: list[str] = []

    def get_redeemers(self, tx_hash: str) -> list[RedeemerRecord]:
        """Recorded redeemers for ``tx_hash``, tracking call order."""
        self.calls.append(f"get_redeemers:{tx_hash}")
        return self.redeemers.get(tx_hash, [])

    def get_pool_utxos(
        self,
        addresses: list[str],
        assets: list[str] | None = None,
        limit: int = 1000,
        page: int = 0,
        historical: bool = True,
    ) -> PoolStateList:
        """The pool NFT's recorded historical states, tracking call order."""
        self.calls.append("get_pool_utxos")
        return PoolStateList(root=list(self.states))


class _NoHistoryBackend(_Minimal):
    """Serves recorded redeemers but cannot list pool history."""

    def __init__(self, redeemers: dict[str, list[RedeemerRecord]]) -> None:
        """Store the redeemer table this backend serves."""
        self.redeemers = redeemers

    def get_redeemers(self, tx_hash: str) -> list[RedeemerRecord]:
        """Recorded redeemers for ``tx_hash``."""
        return self.redeemers.get(tx_hash, [])


# ---------------------------------------------------------------------------
# Settings datums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [pytest.param(e, id=f"{e['label']}-{i}") for i, e in enumerate(_FIX["settings"])],
)
def test_every_settings_datum_with_a_class_round_trips(entry: dict) -> None:
    """Every settings node the parser models decodes and re-encodes byte-exact."""
    cls = _SETTINGS_CLASSES.get(entry["label"])
    if cls is None:
        pytest.skip(f"{entry['label']} has no modelled class")
    cbor = bytes.fromhex(entry["datum"])
    assert cls.from_cbor(cbor).to_cbor() == cbor


# ---------------------------------------------------------------------------
# Pool states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rec", [pytest.param(r, id=r["label"]) for r in _FIX["pool_states"]]
)
def test_pool_states_parse_as_the_mainnet_vault(rec: dict) -> None:
    """Both the creation and latest launch-pool states parse as the vault."""
    vault = SundaeV4Vault.model_validate(_vault_values(rec))
    policy = _DEPLOYMENT.pool_nft_policy.hex()
    identifier = _FIX["identifier"]
    assert vault.identifier.hex() == identifier
    assert vault.pool_nft.unit() == policy + "000de140" + identifier
    assert vault.lp_token.unit() == policy + "0014df10" + identifier
    assert len(vault.datum_units) == 2
    assert [entry.tag for entry in vault.actions] == [100, 200, 1]
    assert vault.surplus == rec["value"]["lovelace"]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rec",
    [pytest.param(r, id=f"order-{i}") for i, r in enumerate(_FIX["orders"])],
)
def test_every_order_datum_round_trips_and_classifies(rec: dict) -> None:
    """Every recorded order round trips and is a basic or a strategy order."""
    cbor = bytes.fromhex(rec["datum"])
    datum = SundaeV4OrderDatum.from_cbor(cbor)
    assert datum.to_cbor() == cbor
    first_key = bytes(list(list(datum.constraints)[0])[0])
    if first_key == _DEPLOYMENT.basic_order_hash:
        assert datum.max_per_execution >= _DEPLOYMENT.base_fee
        assert datum.service_budget >= _DEPLOYMENT.base_fee
    else:
        assert first_key == _DEPLOYMENT.strategy_order_hash


def test_a_recorded_basic_order_re_encodes_byte_exact() -> None:
    """One recorded basic order's own decoded fields rebuild it byte-exact."""
    set_backend(
        _WalkBackend(
            redeemers={_CREATION["tx"]: _records(_FIX["creation_redeemers"])}, states=[]
        )
    )
    vault = SundaeV4Vault.model_validate(_vault_values(_CREATION))
    pool = vault.pools()[0]

    rec = _FIX["orders"][0]
    cbor = bytes.fromhex(rec["datum"])
    datum = SundaeV4OrderDatum.from_cbor(cbor)
    constraints = list(datum.constraints)
    payload = list(constraints[0])[1]
    basic = parse_basic_constraint(payload)
    assert isinstance(datum.destination, DestinationFixed)

    offered = _assets_from_pairs(list(basic.offered))
    min_received = _assets_from_pairs(list(basic.min_received))
    address = datum.destination.address.to_address()
    rebuilt = pool.basic_datum(
        address,
        offered,
        min_received,
        basic.kind,
        address_target=address,
        owner=datum.owner,
        service_budget=datum.service_budget,
        max_per_execution=datum.max_per_execution,
    )
    assert rebuilt.to_cbor() == cbor


# ---------------------------------------------------------------------------
# The history walk
# ---------------------------------------------------------------------------


def test_the_walk_resolves_the_latest_states_config_from_its_creation() -> None:
    """The latest state carries no cs redeemer; the walk finds it at creation."""
    backend = _WalkBackend(
        redeemers={
            _CREATION["tx"]: _records(_FIX["creation_redeemers"]),
            _LATEST["tx"]: _records(_FIX["latest_state_redeemers"]),
        },
        states=[_pool_state_info(_CREATION), _pool_state_info(_LATEST)],
    )
    set_backend(backend)
    vault = SundaeV4Vault.model_validate(_vault_values(_LATEST))

    config = vault.module_config(_CS)
    assert module_config_hash(config) == vault.module_state[_CS]
    assert [int(p) for p in config.prices] == [1, 1]
    assert (config.fee.num, config.fee.den) == (25, 10_000)
    assert (config.bounty_k.num, config.bounty_k.den) == (25, 20_000)

    assert backend.calls[0] == f"get_redeemers:{_LATEST['tx']}"
    assert backend.calls.count("get_pool_utxos") == 1

    pool = vault.pools()[0]
    in_unit, out_unit = pool.prices.keys()
    out, _impact = pool.get_amount_out(Assets(**{in_unit: 100_000_000}), out_unit)
    assert out[out_unit] == 99_750_000


def test_the_creation_state_vault_resolves_without_walking_history() -> None:
    """The creation state's own tx already carries the Create redeemer."""
    backend = _WalkBackend(
        redeemers={_CREATION["tx"]: _records(_FIX["creation_redeemers"])},
        states=[],
    )
    set_backend(backend)
    vault = SundaeV4Vault.model_validate(_vault_values(_CREATION))

    config = vault.module_config(_CS)
    assert module_config_hash(config) == vault.module_state[_CS]
    assert backend.calls.count("get_pool_utxos") == 0


def test_the_walk_raises_once_no_history_state_carries_the_config() -> None:
    """Every historical redeemer is the wrong kind; the walk raises at the end."""
    stale = _records(_FIX["latest_state_redeemers"])
    backend = _WalkBackend(
        redeemers={_CREATION["tx"]: stale, _LATEST["tx"]: stale},
        states=[_pool_state_info(_CREATION), _pool_state_info(_LATEST)],
    )
    set_backend(backend)
    vault = SundaeV4Vault.model_validate(_vault_values(_LATEST))

    with pytest.raises(ModuleConfigUnavailableError):
        vault.module_config(_CS)
    assert backend.calls.count("get_pool_utxos") == 1


def test_a_backend_without_pool_history_raises_module_config_unavailable() -> None:
    """A backend that cannot list pool history maps to ModuleConfigUnavailableError."""
    backend = _NoHistoryBackend(
        {_LATEST["tx"]: _records(_FIX["latest_state_redeemers"])}
    )
    set_backend(backend)
    vault = SundaeV4Vault.model_validate(_vault_values(_LATEST))

    with pytest.raises(ModuleConfigUnavailableError):
        vault.module_config(_CS)
