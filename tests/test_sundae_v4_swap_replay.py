"""Replay real preview-testnet constant-sum swaps through the pool type."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4ConstantSumPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from tests.sundae_v4_vault_factory import build_vault_utxo

_SWAPS = json.loads(
    (Path(__file__).parent / "sundae_v4_swap_replay_fixtures.json").read_text(),
)["swaps"]


@pytest.fixture(autouse=True)
def _restore_default_network() -> Iterator[None]:
    """Guarantee the class family is back on the mainnet default after each test.

    ``_pool()`` points the class family at preview for the recorded replay; this
    restores it regardless of outcome so it cannot leak into the next test.
    """
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")


def _unit(raw: str) -> str:
    """The dendrite unit for a fixture's on-chain unit string (``""`` is ADA)."""
    return raw or "lovelace"


def _pool(swap: dict) -> SundaeV4ConstantSumPool:
    """A two-asset vault in the recorded before-state, priced on the recorded config."""
    values, config = build_vault_utxo(
        [
            (_unit(swap["in_unit"]), swap["in_reserve"]),
            (_unit(swap["out_unit"]), swap["out_reserve"]),
        ],
        prices=[swap["price_in"], swap["price_out"]],
        fee=(swap["fee_num"], swap["fee_den"]),
        bounty_k=(swap["bounty_num"], swap["bounty_den"]),
        balance_fee=(swap["balance_fee_num"], swap["balance_fee_den"]),
        total_lp=swap["in_reserve"] * swap["price_in"]
        + swap["out_reserve"] * swap["price_out"],
        identifier=bytes.fromhex(swap["ident"]),
    )
    SundaeV4Vault.select_network("preview")
    vault = SundaeV4Vault.model_validate(values)
    cs = SundaeV4Deployment.for_network("preview").validator("constant_sum.withdraw")
    vault.supply_module_config(cs, config)
    return vault.pools()[0]


def test_replay_fixture_is_non_trivial() -> None:
    """Guard against a fixture that regenerated to nothing."""
    assert len(_SWAPS) >= 100
    assert any(s["price_in"] != s["price_out"] for s in _SWAPS)
    assert any(s["bounty_num"] > 0 for s in _SWAPS)
    required = {"bounty_num", "bounty_den", "balance_fee_num", "balance_fee_den"}
    assert all(required <= s.keys() for s in _SWAPS)


@pytest.mark.parametrize(
    "swap",
    _SWAPS,
    ids=[f"{s['scoop_tx'][:8]}:{s['step']}" for s in _SWAPS],
)
def test_get_amount_out_reproduces_the_real_payout(swap: dict) -> None:
    pool = _pool(swap)
    in_unit, out_unit = _unit(swap["in_unit"]), _unit(swap["out_unit"])
    out, _ = pool.get_amount_out(Assets(**{in_unit: swap["amount_in"]}), out_unit)
    assert out.unit() == out_unit
    assert out.quantity() == swap["expected_out"]


@pytest.mark.parametrize(
    "swap",
    _SWAPS,
    ids=[f"{s['scoop_tx'][:8]}:{s['step']}" for s in _SWAPS],
)
def test_get_amount_in_inverts_the_real_payout(swap: dict) -> None:
    pool = _pool(swap)
    in_unit, out_unit = _unit(swap["in_unit"]), _unit(swap["out_unit"])
    needed, _ = pool.get_amount_in(Assets(**{out_unit: swap["expected_out"]}), in_unit)
    assert needed.quantity() <= swap["amount_in"]
    assert (
        pool.get_amount_out(Assets(**{in_unit: needed.quantity()}), out_unit)[
            0
        ].quantity()
        >= swap["expected_out"]
    )
