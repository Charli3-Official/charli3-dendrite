"""Replay real preview-testnet constant-sum swaps through ``get_amount_out``.

Each fixture entry is an *actual on-chain* swap step: the pool's before-reserves,
its constant-sum price weights and fee, and the input the scooper received, paired
with the output it actually paid. The dendrite constant-sum ``get_amount_out``
must reproduce the real output exactly on every step, including pools whose
price weights are not 1:1. The fixture was captured from the live preview
deployment, so the replay needs no network at test time.
"""

import json
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CSState

_SWAPS = json.loads(
    (Path(__file__).parent / "sundae_v4_swap_replay_fixtures.json").read_text(),
)["swaps"]


def _leg(swap: dict) -> _SundaeV4CSState:
    """Project the 2-asset constant-sum leg the swap executed against.

    The leg's ``price_a`` / ``price_b`` follow the canonical ``(unit_a, unit_b)``
    ordering the state applies to its assets, so the price weights are aligned
    to the units the fixture names rather than to the swap direction.
    """
    prices = {swap["in_unit"]: swap["price_in"], swap["out_unit"]: swap["price_out"]}
    unit_a, unit_b = sorted(prices)
    return _SundaeV4CSState.model_validate(
        {
            "assets": Assets(
                **{
                    swap["in_unit"]: swap["in_reserve"],
                    swap["out_unit"]: swap["out_reserve"],
                },
            ),
            "block_time": 0,
            "block_index": 0,
            "plutus_v2": True,
            "datum_cbor": "00",
            "datum_hash": "00",
            "tx_index": 0,
            "tx_hash": "00",
            "price_a": prices[unit_a],
            "price_b": prices[unit_b],
            "fee_numerator": swap["fee_num"],
            "fee_denominator": swap["fee_den"],
        },
    )


def test_replay_fixture_is_non_trivial() -> None:
    """Guard against a fixture that regenerated to nothing."""
    assert len(_SWAPS) >= 100
    assert any(s["price_in"] != s["price_out"] for s in _SWAPS)


@pytest.mark.parametrize(
    "swap",
    _SWAPS,
    ids=[f"{s['scoop_tx'][:8]}#{s['step']}" for s in _SWAPS],
)
def test_get_amount_out_replays_real_swap(swap: dict) -> None:
    """get_amount_out reproduces the exact output a real on-chain swap paid."""
    out_assets, _impact = _leg(swap).get_amount_out(
        Assets(**{swap["in_unit"]: swap["amount_in"]}),
    )
    assert out_assets.quantity() == swap["expected_out"]
