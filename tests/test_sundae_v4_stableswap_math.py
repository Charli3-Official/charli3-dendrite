"""Exact stableswap math against every recorded step and the validator oracle."""

from __future__ import annotations

import math
import random

import pytest

from charli3_dendrite.dexs.amm.sundae_v4 import PoolAction
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapOperate
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import parse_stableswap_step
from charli3_dendrite.dexs.amm.sundae_v4_stableswap_math import STABLESWAP_PRECISION
from charli3_dendrite.dexs.amm.sundae_v4_stableswap_math import stableswap_d
from charli3_dendrite.dexs.amm.sundae_v4_stableswap_math import stableswap_swap
from tests.sundae_v4_ss_oracle import check_swap
from tests.sundae_v4_ss_oracle import liquidity_invariant
from tests.test_sundae_v4_stableswap_types import MAINNET
from tests.test_sundae_v4_stableswap_types import vault_steps

P = STABLESWAP_PRECISION
_AMP, _FEE = 500, (15, 10_000)


def _reserves(datum_hex: str) -> list[int]:
    return [int(list(e)[1]) for e in SundaeV4PoolDatum.from_cbor(datum_hex).assets]


def _operate_entry(tx: str):
    for r in MAINNET["redeemers"][tx]:
        if r["script_hash"] == MAINNET["module"]:
            (entry,) = StableSwapOperate.from_cbor(r["data_cbor"]).entries
            return entry
    raise AssertionError(tx)


def test_every_recorded_mainnet_step_reproduces() -> None:
    states = MAINNET["pool_states"]
    swaps = 0
    for prev, state in zip(states, states[1:]):
        entry = _operate_entry(state["tx"])
        rates = [int(r) for r in entry.config.rates]
        before = _reserves(prev["datum"])
        d = stableswap_d(_AMP, before[0] * rates[0] * P, before[1] * rates[1] * P)
        assert d == entry.sum_invariant
        for step in vault_steps(state["tx"], state["index"]):
            after = [int(list(e)[1]) for e in step.state_after.assets]
            payload = parse_stableswap_step(step.operation_tag, step.operation_data)
            if step.operation_tag == 7:
                rates = [int(r) for r in payload.rates]
            elif step.operation_tag == 3:
                i = 0 if after[0] > before[0] else 1
                o = 1 - i
                quote = stableswap_swap(
                    _AMP,
                    d,
                    before[i],
                    rates[i],
                    before[o],
                    rates[o],
                    after[i] - before[i],
                    _FEE,
                )
                assert quote.raw_swap_result == payload.raw_swap_result
                assert quote.takes == before[o] - after[o]
                swaps += 1
            next_d = stableswap_d(
                _AMP, after[0] * rates[0] * P, after[1] * rates[1] * P
            )
            assert next_d == payload.next_sum_invariant
            before, d = after, next_d
    assert swaps == 4


# Recorded preview steps (USDr/sUSDr, A=200, fee 25/10000): before, rates, after,
# D before, raw swap result, D after — from the preview pools' own transcripts.
_PREVIEW_SWAPS = [
    (
        [10_219_890_000, 9_780_671_753],
        [1_000_000, 1_000_000],
        [11_218_890_000, 8_784_532_068],
        20000549720582444975935611454,
        998636276858159519275791292,
        20003047141709075907028748434,
    ),
    (
        [10_695_001_263, 10_285_820_858],
        [1_000_000, 1_001_000],
        [10_794_901_263, 10_186_281_970],
        20991098486829941094949407134,
        99888147376791386862936545,
        20991348225358657993984399717,
    ),
    (
        [10_595_001_263, 10_385_464_121],
        [1_000_000, 1_001_000],
        [10_695_001_263, 10_285_820_858],
        20990848491973490189010679074,
        99992889147916277844231960,
        20991098486829941094949407134,
    ),
]


@pytest.mark.parametrize(
    ("before", "rates", "after", "d0", "raw", "d1"), _PREVIEW_SWAPS
)
def test_recorded_preview_swaps_reproduce(before, rates, after, d0, raw, d1) -> None:
    assert stableswap_d(200, before[0] * rates[0] * P, before[1] * rates[1] * P) == d0
    quote = stableswap_swap(
        200,
        d0,
        before[0],
        rates[0],
        before[1],
        rates[1],
        after[0] - before[0],
        (25, 10_000),
    )
    assert quote.raw_swap_result == raw
    assert quote.takes == before[1] - after[1]
    assert stableswap_d(200, after[0] * rates[0] * P, after[1] * rates[1] * P) == d1


def test_quotes_pass_the_validator_and_one_more_unit_fails() -> None:
    rng = random.Random(882)
    for _ in range(2_000):
        amp = rng.choice([1, 10, 200, 500, 10_000])
        rates = [
            rng.choice([1, 100, 10**6, 1_001_000]),
            rng.choice([1, 10**6, 1_000_001, 1_033_648]),
        ]
        before = [rng.randint(1, 10**13), rng.randint(1, 10**13)]
        fee = rng.choice([(0, 1), (5, 10_000), (15, 10_000), (1, 100)])
        amount = int(math.exp(rng.uniform(0, math.log(before[0] * 5 + 2))))
        d = stableswap_d(amp, before[0] * rates[0] * P, before[1] * rates[1] * P)
        assert liquidity_invariant(
            before[0] * rates[0] * P, before[1] * rates[1] * P, amp, d
        )
        quote = stableswap_swap(
            amp, d, before[0], rates[0], before[1], rates[1], amount, fee
        )
        after = [before[0] + amount, before[1] - quote.takes]
        next_d = stableswap_d(amp, after[0] * rates[0] * P, after[1] * rates[1] * P)
        if quote.takes > 0:
            assert check_swap(
                before, after, rates, quote.raw_swap_result, d, next_d, amp, fee
            )
        greedy = [before[0] + amount, before[1] - quote.takes - 1]
        greedy_d = stableswap_d(amp, greedy[0] * rates[0] * P, greedy[1] * rates[1] * P)
        assert not check_swap(
            before, greedy, rates, quote.raw_swap_result, d, greedy_d, amp, fee
        )


def test_an_empty_side_has_no_invariant() -> None:
    assert stableswap_d(500, 0, 10**20) == 0
    assert stableswap_d(500, 10**20, 0) == 0
