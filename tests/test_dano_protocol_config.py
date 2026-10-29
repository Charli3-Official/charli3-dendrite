"""Dano protocol config: located by its NFT, its swap fee is the batcher fee.

Every Dano swap pays the protocol config's ``swap_fee`` (lovelace) into the
pool it spends, so ``batcher_fee`` reports that fee. The config UTxO is found
by the NFT it holds, both through the backend (for the fee) and among a
transaction's reference inputs (for building the swap), so a move of the
config UTxO does not break either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import charli3_dendrite.backend as backend_module
import pytest
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dexs.amm import dano
from charli3_dendrite.dexs.amm.dano import PROTOCOL_CONFIG_ADDRESS_MAINNET
from charli3_dendrite.dexs.amm.dano import PROTOCOL_CONFIG_NFT_NAME_MAINNET
from charli3_dendrite.dexs.amm.dano import PROTOCOL_CONFIG_NFT_POLICY_MAINNET
from charli3_dendrite.dexs.amm.dano import PROTOCOL_CONFIG_NFT_UNIT_MAINNET
from charli3_dendrite.dexs.amm.dano import PROTOCOL_CONFIG_OUT_REF_MAINNET
from charli3_dendrite.dexs.amm.dano import DanoCLMMState
from charli3_dendrite.dexs.amm.dano import DanoSwapRedeemer
from charli3_dendrite.dexs.amm.dano import _find_protocol_config
from pycardano import Address
from pycardano import MultiAsset
from pycardano import PlutusData
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value

_FIX = json.loads(
    (Path(__file__).parent / "data" / "dano_pool_fixtures.json").read_text(),
)

MOVED_TX = "ab" * 32
PLATFORM_FEE_RATE = 1000
SWAP_FEE = 750_000
NEW_SWAP_FEE = 900_000


@dataclass
class _ConfigDatum(PlutusData):
    CONSTR_ID = 0
    platform_fee_rate: int
    swap_fee: int


def _config_cbor(platform_fee_rate: int, swap_fee: int) -> str:
    return _ConfigDatum(platform_fee_rate, swap_fee).to_cbor_hex()


class _FakeBackend:
    """Answers the protocol-config lookup; records every call."""

    def __init__(self, swap_fee: int, tx_hash: str = MOVED_TX) -> None:
        self.swap_fee = swap_fee
        self.tx_hash = tx_hash
        self.calls: list[tuple[str, str | None]] = []

    def get_datum_from_address(
        self,
        address: Address,
        asset: str | None = None,
    ) -> ScriptReference | None:
        self.calls.append((str(address), asset))
        return ScriptReference(
            tx_hash=self.tx_hash,
            tx_index=0,
            address=str(address),
            assets=Assets(
                root={"lovelace": 2_000_000, PROTOCOL_CONFIG_NFT_UNIT_MAINNET: 1},
            ),
            datum_hash=None,
            datum_cbor=_config_cbor(PLATFORM_FEE_RATE, self.swap_fee),
            script=None,
        )


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """A controllable clock; the protocol-config cache starts empty."""
    c = _Clock()
    monkeypatch.setattr(dano, "_clock", c)
    monkeypatch.setattr(DanoCLMMState, "_protocol_config", dano.PINNED_PROTOCOL_CONFIG)
    monkeypatch.setattr(DanoCLMMState, "_protocol_config_checked_at", None)
    return c


def _use_backend(monkeypatch: pytest.MonkeyPatch, backend: object | None) -> None:
    monkeypatch.setattr(backend_module, "BACKEND", backend)
    monkeypatch.setattr(backend_module, "set_default_backend", lambda: None)


def _pool(key: str = "both_reserve") -> DanoCLMMState:
    return DanoCLMMState.model_validate(_FIX[key]["row"])


def test_constants_name_the_config_nft_and_address() -> None:
    """The config NFT, address and last known out-ref."""
    assert PROTOCOL_CONFIG_NFT_POLICY_MAINNET == (
        "bc2d3b7cf1009c788b1daf2208a38026ab20d7f8209d896ad1a09c14"
    )
    assert PROTOCOL_CONFIG_NFT_NAME_MAINNET == (
        "72f9dae79bb2dfa39bd8fadf089ea0c8377b5eaa3caef0549396668aef1f9187"
    )
    assert PROTOCOL_CONFIG_NFT_UNIT_MAINNET == (
        PROTOCOL_CONFIG_NFT_POLICY_MAINNET + PROTOCOL_CONFIG_NFT_NAME_MAINNET
    )
    assert PROTOCOL_CONFIG_ADDRESS_MAINNET == (
        "addr1z8afjx7zl8zzqmnj6ufmcdy85uh8jqg90j4m35myh6lwlrectd0zw3ydwgtk9hpse3mgyezedms0y054fa47vfkt08qsdxsvyq"
    )
    assert PROTOCOL_CONFIG_OUT_REF_MAINNET == (
        "67380365f2c95ebeb2feaef321c29462550e54b9bd888ae453170e679a8914a0",
        0,
    )


@pytest.mark.usefixtures("clock")
def test_batcher_fee_is_the_config_swap_fee(monkeypatch: pytest.MonkeyPatch) -> None:
    """The batcher fee is the swap fee of the config found by its NFT."""
    fake = _FakeBackend(swap_fee=SWAP_FEE)
    _use_backend(monkeypatch, fake)

    assert _pool().batcher_fee() == Assets(lovelace=SWAP_FEE)
    assert fake.calls == [
        (PROTOCOL_CONFIG_ADDRESS_MAINNET, PROTOCOL_CONFIG_NFT_UNIT_MAINNET),
    ]
    config = DanoCLMMState.protocol_config()
    assert config.out_ref == (MOVED_TX, 0)
    assert config.platform_fee_rate == PLATFORM_FEE_RATE
    assert config.swap_fee == SWAP_FEE


def test_config_is_cached_until_the_ttl_expires(
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read is reused until the TTL expires, then refreshed."""
    fake = _FakeBackend(swap_fee=SWAP_FEE)
    _use_backend(monkeypatch, fake)
    pool = _pool()

    assert pool.batcher_fee() == Assets(lovelace=SWAP_FEE)
    fake.swap_fee = NEW_SWAP_FEE
    clock.now += dano.PROTOCOL_CONFIG_CACHE_TTL_SECONDS - 1
    assert pool.batcher_fee() == Assets(lovelace=SWAP_FEE)
    assert len(fake.calls) == 1

    clock.now += 2
    assert pool.batcher_fee() == Assets(lovelace=NEW_SWAP_FEE)
    assert len(fake.calls) == 2  # noqa: PLR2004


@pytest.mark.usefixtures("clock")
def test_fallback_without_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a backend the pinned config applies."""
    _use_backend(monkeypatch, None)

    assert _pool().batcher_fee() == Assets(lovelace=500_000)
    assert DanoCLMMState._batcher == Assets(lovelace=500_000)
    assert DanoCLMMState.protocol_config().out_ref == PROTOCOL_CONFIG_OUT_REF_MAINNET


def test_keeps_the_last_known_config_when_the_backend_has_no_answer(
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No answer or a failed read keeps the last known config."""
    fake = _FakeBackend(swap_fee=SWAP_FEE)
    _use_backend(monkeypatch, fake)
    pool = _pool()
    assert pool.batcher_fee() == Assets(lovelace=SWAP_FEE)

    def _no_answer(address: Address, asset: str | None = None) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(fake, "get_datum_from_address", _no_answer)
    clock.now += dano.PROTOCOL_CONFIG_CACHE_TTL_SECONDS + 1
    assert pool.batcher_fee() == Assets(lovelace=SWAP_FEE)

    def _fails(address: Address, asset: str | None = None) -> None:  # noqa: ARG001
        raise ConnectionError("backend unavailable")

    monkeypatch.setattr(fake, "get_datum_from_address", _fails)
    clock.now += dano.PROTOCOL_CONFIG_CACHE_TTL_SECONDS + 1
    assert pool.batcher_fee() == Assets(lovelace=SWAP_FEE)


def _config_utxo(
    tx_hash: str,
    *,
    with_nft: bool,
    swap_fee: int = 500_000,
) -> UTxO:
    multi_asset = MultiAsset.from_primitive(
        {
            bytes.fromhex(PROTOCOL_CONFIG_NFT_POLICY_MAINNET): {
                bytes.fromhex(PROTOCOL_CONFIG_NFT_NAME_MAINNET): 1,
            },
        },
    )
    amount = Value(2_000_000, multi_asset) if with_nft else Value(2_000_000)
    return UTxO(
        input=TransactionInput(TransactionId(bytes.fromhex(tx_hash)), 0),
        output=TransactionOutput(
            address=Address.decode(PROTOCOL_CONFIG_ADDRESS_MAINNET),
            amount=amount,
            datum=_ConfigDatum(PLATFORM_FEE_RATE, swap_fee),
        ),
    )


class _Context:
    """A chain context that cannot resolve UTxOs by out-ref."""


def _builder(*reference_inputs: UTxO | TransactionInput) -> TransactionBuilder:
    tb = TransactionBuilder(_Context())
    for ref in reference_inputs:
        tb.reference_inputs.add(ref)
    return tb


def test_find_protocol_config_picks_the_nft_holder() -> None:
    """The NFT holder is picked among several reference inputs."""
    holder = _config_utxo(MOVED_TX, with_nft=True)
    tb = _builder(
        _config_utxo("cd" * 32, with_nft=False),
        TransactionInput(TransactionId(bytes.fromhex("ef" * 32)), 1),
        holder,
        _config_utxo(PROTOCOL_CONFIG_OUT_REF_MAINNET[0], with_nft=False),
    )

    assert _find_protocol_config(tb) is holder


def test_find_protocol_config_none_without_the_nft() -> None:
    """No reference input holds the NFT: no config, even at the pinned out-ref."""
    tb = _builder(
        _config_utxo("cd" * 32, with_nft=False),
        _config_utxo(PROTOCOL_CONFIG_OUT_REF_MAINNET[0], with_nft=False),
    )

    assert _find_protocol_config(tb) is None


class _RecordingBuilder(TransactionBuilder):
    """Accepts the pool spend; records the withdraw-zero redeemer."""

    def add_script_input(self, *args: object, **kwargs: object) -> _RecordingBuilder:
        return self

    def add_withdrawal_script(
        self,
        script: object,
        redeemer: Redeemer | None = None,
    ) -> _RecordingBuilder:
        self.recorded_withdraw = redeemer
        return self


@dataclass
class _PoolTx:
    address: str


class _SwapBackend:
    def get_pool_in_tx(
        self,
        tx_hash: str,
        assets: list[str],
        addresses: list[str],
    ) -> list[_PoolTx]:
        return [_PoolTx(address=addresses[1])]


def _swap(
    monkeypatch: pytest.MonkeyPatch,
    *reference_inputs: UTxO | TransactionInput,
) -> tuple[DanoCLMMState, _RecordingBuilder, TransactionOutput, PlutusData]:
    _use_backend(monkeypatch, _SwapBackend())
    monkeypatch.setattr(
        DanoCLMMState,
        "_reference_utxo",
        _config_utxo("12" * 32, with_nft=False),
    )
    pool = _pool("both_reserve")
    tb = _RecordingBuilder(_Context())
    for ref in reference_inputs:
        tb.reference_inputs.add(ref)
    d = pool._datum
    out, datum = pool.swap_utxo(
        address_source=Address.decode(PROTOCOL_CONFIG_ADDRESS_MAINNET),
        in_assets=Assets(root={d.unit_x: 1_000_000}),
        out_assets=Assets(root={d.unit_y: 1}),
        tx_builder=tb,
    )
    return pool, tb, out, datum


def test_swap_utxo_builds_against_a_moved_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swap builds against the config at a new out-ref, using its swap fee."""
    moved = _config_utxo(MOVED_TX, with_nft=True, swap_fee=600_000)
    pool, tb, out, datum = _swap(
        monkeypatch,
        _config_utxo(PROTOCOL_CONFIG_OUT_REF_MAINNET[0], with_nft=False),
        moved,
    )

    assert datum.total_swap_fee == pool._datum.total_swap_fee + 600_000
    assert pool.platform_fee_rate == PLATFORM_FEE_RATE
    assert out.amount.coin == pool._gross_assets()["lovelace"] + 600_000
    withdraw = tb.recorded_withdraw.data
    assert isinstance(withdraw, DanoSwapRedeemer)
    assert withdraw.protocol_config_input == moved.input


def test_swap_utxo_without_the_config_names_the_nft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing config is reported by its NFT."""
    with pytest.raises(ValueError, match=PROTOCOL_CONFIG_NFT_UNIT_MAINNET):
        _swap(
            monkeypatch,
            _config_utxo(PROTOCOL_CONFIG_OUT_REF_MAINNET[0], with_nft=False),
        )
