"""SundaeV4StableSwapPool against the recorded mainnet vault and the validator oracle."""

from __future__ import annotations

import math
import random
from collections.abc import Iterator

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.multi_asset import AbstractMultiAssetPoolState
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapConfig
from charli3_dendrite.dexs.amm.sundae_v4 import StableSwapCreate
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4StableSwapPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.amm.sundae_v4 import parse_stableswap_step
from charli3_dendrite.dexs.amm.sundae_v4_stableswap_math import STABLESWAP_PRECISION
from charli3_dendrite.dexs.amm.sundae_v4_stableswap_math import stableswap_d
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from tests.sundae_v4_ss_oracle import check_liquidity
from tests.sundae_v4_ss_oracle import check_swap
from tests.sundae_v4_vault_factory import build_stableswap_vault_utxo
from tests.test_sundae_v4_stableswap_math import _operate_entry
from tests.test_sundae_v4_stableswap_types import MAINNET
from tests.test_sundae_v4_stableswap_types import vault_steps

P = STABLESWAP_PRECISION
USDR = "7d9e4a0ee1a3f5d5ff8159ea91a83310cf2795ee7a87170c7aea05ae55534472"
SUSDR = "7d9e4a0ee1a3f5d5ff8159ea91a83310cf2795ee7a87170c7aea05ae7355534472"
_LEDGER_MAX = 2**64 - 1


@pytest.fixture(autouse=True)
def _mainnet() -> Iterator[None]:
    SundaeV4Vault.select_network("mainnet")
    SundaeV4Vault.clear_config_cache()
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_config_cache()


def _config_for(index: int) -> StableSwapConfig:
    """The config state ``index`` commits to: the next scoop's input, else the Create."""
    states = MAINNET["pool_states"]
    if index + 1 < len(states):
        return _operate_entry(states[index + 1]["tx"]).config
    (create,) = [
        r
        for r in MAINNET["redeemers"][states[0]["tx"]]
        if r["script_hash"] == MAINNET["module"]
    ]
    return StableSwapCreate.from_cbor(create["data_cbor"]).initial_state


def _pool(index: int) -> SundaeV4StableSwapPool:
    state = MAINNET["pool_states"][index]
    vault = SundaeV4Vault.model_validate(
        {
            "tx_hash": state["tx"],
            "tx_index": state["index"],
            "datum_cbor": state["datum"],
            "assets": state["value"],
            "block_time": state["block_time"],
        }
    )
    vault.supply_module_config(bytes.fromhex(MAINNET["module"]), _config_for(index))
    (pool,) = vault.pools()
    assert isinstance(pool, SundaeV4StableSwapPool)
    return pool


def _synthetic(reserves, rates, amp=500, fee=(15, 10_000)) -> SundaeV4StableSwapPool:
    SundaeV4Vault.select_network("preview")
    units = ["01" * 28 + "01", "02" * 28 + "02"]
    values, config = build_stableswap_vault_utxo(
        list(zip(units, reserves)), rates=rates, amp=amp, fee=fee, total_lp=10**12
    )
    vault = SundaeV4Vault.model_validate(values)
    vault.supply_module_config(
        SundaeV4Deployment.for_network("preview").stableswap_hash, config
    )
    (pool,) = vault.pools()
    assert isinstance(pool, AbstractMultiAssetPoolState)
    return pool


def _reserves(datum_hex: str) -> list[int]:
    return [int(list(e)[1]) for e in SundaeV4PoolDatum.from_cbor(datum_hex).assets]


def test_the_live_vault_is_one_stableswap_pool() -> None:
    pool = _pool(10)
    assert pool.tag == 100
    assert pool.pool_id == f"{pool.vault.pool_id}:100"
    assert pool.units() == [USDR, SUSDR]
    assert pool.rates == {USDR: 1_000_000, SUSDR: 1_000_000}
    assert pool.amp == 500
    assert pool.rate_manager is not None
    assert pool.monotone_rates is False
    assert pool.max_rate_step is None
    assert pool.dex() == "SundaeSwapV4"
    assert pool.stake_address == SundaeV4Deployment.for_network("mainnet").order_address
    assert pool.deposit().quantity() == 2_000_000


def test_every_recorded_swap_reproduces_from_the_state_before_it() -> None:
    states = MAINNET["pool_states"]
    checked = 0
    for i in range(len(states) - 1):
        steps = vault_steps(states[i + 1]["tx"], states[i + 1]["index"])
        if not steps or steps[0].operation_tag != 3:
            continue
        pool = _pool(i)
        assert pool.sum_invariant() == _operate_entry(states[i + 1]["tx"]).sum_invariant
        before = _reserves(states[i]["datum"])
        after = [int(list(e)[1]) for e in steps[0].state_after.assets]
        units = pool.vault.datum_units
        i_in = 0 if after[0] > before[0] else 1
        offer = Assets(**{units[i_in]: after[i_in] - before[i_in]})
        out, impact = pool.get_amount_out(offer, units[1 - i_in])
        assert out.quantity() == before[1 - i_in] - after[1 - i_in]
        assert 0.0 < impact < 0.01
        checked += 1
    assert checked == 4


def test_recorded_liquidity_steps_are_the_pinned_moves() -> None:
    states = MAINNET["pool_states"]
    offered_by_state = {
        4: [5_000_000, 5_293_880],
        5: [2_361_218, 2_500_000],
        7: [4_000_000, 3_347_661],
    }
    for i, offered in offered_by_state.items():
        pool = _pool(i - 1)
        (step,) = vault_steps(states[i]["tx"], states[i]["index"])
        payload = parse_stableswap_step(step.operation_tag, step.operation_data)
        pinned = pool.pinned_deposit(
            Assets(**dict(zip(pool.vault.datum_units, offered)))
        )
        assert pinned.target_delta_v == payload.target_delta_d
        before = _reserves(states[i - 1]["datum"])
        assert [b + d for b, d in zip(before, pinned.deltas)] == _reserves(
            states[i]["datum"]
        )
        assert pinned.lp_after == step.state_after.total_lp
    pool = _pool(7)
    (step,) = vault_steps(states[8]["tx"], states[8]["index"])
    payload = parse_stableswap_step(step.operation_tag, step.operation_data)
    burned = pool.total_lp - step.state_after.total_lp
    withdraw = pool.pinned_withdraw(burned)
    assert withdraw.target_delta_v == payload.target_delta_d
    assert withdraw.lp_after == step.state_after.total_lp
    before = _reserves(states[7]["datum"])
    assert [b - p for b, p in zip(before, withdraw.payouts)] == _reserves(
        states[8]["datum"]
    )


def test_quotes_pass_the_validator_oracle_and_one_more_unit_fails() -> None:
    rng = random.Random(4882)
    for _ in range(400):
        rates = [rng.choice([1, 100, 10**6]), rng.choice([1, 10**6, 1_000_001])]
        reserves = [rng.randint(10, 10**12), rng.randint(10, 10**12)]
        pool = _synthetic(reserves, rates, amp=rng.choice([1, 200, 10_000]))
        a, b = pool.vault.datum_units
        amount = int(math.exp(rng.uniform(0, math.log(reserves[0] * 3 + 2))))
        takes = pool.get_amount_out(Assets(**{a: amount}), b)[0].quantity()
        d = pool.sum_invariant()
        after = [reserves[0] + amount, reserves[1] - takes]
        next_d = stableswap_d(
            pool.amp, after[0] * rates[0] * P, after[1] * rates[1] * P
        )
        raw = reserves[1] * rates[1] * P - _scaled_after(pool, a, amount)
        if takes > 0:
            assert check_swap(
                reserves, after, rates, raw, d, next_d, pool.amp, (15, 10_000)
            )
        greedy = [after[0], after[1] - 1]
        g_d = stableswap_d(pool.amp, greedy[0] * rates[0] * P, greedy[1] * rates[1] * P)
        assert not check_swap(
            reserves, greedy, rates, raw, d, g_d, pool.amp, (15, 10_000)
        )


def _scaled_after(pool: SundaeV4StableSwapPool, unit_in: str, amount: int) -> int:
    from charli3_dendrite.dexs.amm.sundae_v4_stableswap_math import stableswap_y

    x = (pool.reserves.root[unit_in] + amount) * pool.rates[unit_in] * P
    return stableswap_y(pool.amp, pool.sum_invariant(), x)


def test_an_empty_side_quotes_zero_and_reports_its_peg() -> None:
    pool = _synthetic([10**9, 0], [1_000_000, 1_000_000])
    a, b = pool.vault.datum_units
    assert pool.sum_invariant() == 0
    assert pool.get_amount_out(Assets(**{a: 10**6}), b)[0].quantity() == 0
    assert pool.get_amount_out(Assets(**{b: 10**6}), a)[0].quantity() == 0
    assert pool.price(a, b) == (1_000_000, 1_000_000)
    assert pool.max_output(b) == 1
    with pytest.raises(InvalidPoolError):
        pool.get_amount_in(Assets(**{b: 1}), a)


def test_get_amount_in_is_the_exact_minimum_up_to_the_ceiling() -> None:
    rng = random.Random(7)
    for _ in range(120):
        rates = [rng.choice([1, 100, 10**6]), rng.choice([1, 10**6, 1_000_001])]
        pool = _synthetic(
            [rng.randint(10**3, 10**12), rng.randint(10**3, 10**12)], rates
        )
        a, b = pool.vault.datum_units
        ceiling = pool.max_output(b)
        assert (
            ceiling
            == pool.get_amount_out(Assets(**{a: _LEDGER_MAX}), b)[0].quantity() + 1
        )
        desired = rng.randint(1, ceiling - 1)
        needed = pool.get_amount_in(Assets(**{b: desired}), a)[0].quantity()
        assert pool.get_amount_out(Assets(**{a: needed}), b)[0].quantity() >= desired
        if needed > 1:
            assert (
                pool.get_amount_out(Assets(**{a: needed - 1}), b)[0].quantity()
                < desired
            )
        with pytest.raises(InvalidPoolError):
            pool.get_amount_in(Assets(**{b: ceiling}), a)


def test_price_is_the_marginal_rate() -> None:
    p_in, p_out = _pool(10).price(USDR, SUSDR)
    assert p_in / p_out == pytest.approx(0.9996033049, abs=1e-9)
    balanced = _synthetic([10**9, 10**9], [1_000_000, 1_000_000])
    a, b = balanced.vault.datum_units
    w_a, w_b = balanced.price(a, b)
    assert w_a == w_b


def test_apply_swap_moves_the_snapshot_not_the_vault() -> None:
    pool = _pool(10)
    before = dict(pool.vault.reserves.root)
    d0 = pool.sum_invariant()
    pool.apply_swap(Assets(**{USDR: 1_000_000}), Assets(**{SUSDR: 999_000}))
    assert pool.reserves.root[USDR] == before[USDR] + 1_000_000
    assert pool.vault.reserves.root == before
    assert pool.sum_invariant() != d0
