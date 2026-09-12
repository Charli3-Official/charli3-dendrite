"""Replay real preview-testnet constant-sum deposits through the pinned-deposit rule.

A constant-sum deposit is target-pinned on chain: the step declares a value
delta ``t`` and every reserve must move by ``ceil(r_i * t / V_b)`` while total LP
moves to ``floor(lp_b * (V_b + t) / V_b)``. Each fixture entry is an actual
on-chain deposit step; :func:`constant_sum_pinned_deposit` must reproduce the
declared target, the per-asset contributions and the LP minted exactly.
"""

import json
from pathlib import Path

import pytest

from charli3_dendrite.dexs.amm.sundae_v4 import constant_sum_pinned_deposit

_DEPOSITS = json.loads(
    (Path(__file__).parent / "sundae_v4_deposit_fixtures.json").read_text(),
)["deposits"]


@pytest.mark.parametrize(
    "deposit",
    _DEPOSITS,
    ids=[f"{d['scoop_tx'][:8]}#{d['step']}" for d in _DEPOSITS],
)
def test_pinned_deposit_replays_real_deposit(deposit: dict) -> None:
    """The pinned rule reproduces the reserve deltas and LP minted on chain."""
    before = deposit["reserves_before"]
    after = deposit["reserves_after"]
    offered = [a - b for b, a in zip(before, after)]
    result = constant_sum_pinned_deposit(
        reserves=before,
        prices=deposit["prices"],
        offered=offered,
        total_lp=deposit["lp_before"],
    )
    assert result.target_delta_v == deposit["target_delta_v"]
    assert result.deltas == offered
    assert result.lp_after == deposit["lp_after"]


def test_pinned_deposit_is_capped_by_the_scarcest_offered_asset() -> None:
    """Over-offering one asset does not raise the target; the excess is change."""
    result = constant_sum_pinned_deposit(
        reserves=[1_000, 1_000],
        prices=[1, 1],
        offered=[100, 500],
        total_lp=2_000,
    )
    assert result.target_delta_v == 200
    assert result.deltas == [100, 100]
    assert result.lp_after == 2_200  # a 10% value deposit mints 10% LP


def test_pinned_deposit_requires_every_asset() -> None:
    with pytest.raises(ValueError, match="every pool asset"):
        constant_sum_pinned_deposit(
            reserves=[10, 10], prices=[1, 1], offered=[5, 0], total_lp=20
        )
