"""SundaeV4ConstantSumPool: the N-asset constant-sum pool type bound to a vault."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.multi_asset import AbstractMultiAssetPoolState
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4ConstantSumPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from tests.sundae_v4_cs_oracle import check_deposit
from tests.sundae_v4_cs_oracle import check_swap
from tests.sundae_v4_cs_oracle import check_withdraw
from tests.sundae_v4_vault_factory import build_vault_utxo

_DEPOSITS = json.loads(
    (Path(__file__).parent / "sundae_v4_deposit_fixtures.json").read_text(),
)["deposits"]


def _units(n: int) -> list[str]:
    return [f"{i:02x}" * 28 + f"{i:02x}" for i in range(1, n + 1)]


def _pool(
    reserves: list[tuple[str, int]],
    prices: list[int],
    *,
    fee: tuple[int, int] = (3, 1000),
    bounty_k: tuple[int, int] = (0, 1),
    total_lp: int | None = None,
    tags: dict[int, list[str]] | None = None,
) -> SundaeV4ConstantSumPool:
    values, config = build_vault_utxo(
        reserves,
        prices=prices,
        fee=fee,
        bounty_k=bounty_k,
        total_lp=total_lp
        if total_lp is not None
        else sum(r * p for (_, r), p in zip(reserves, prices)),
        tags=tags,
    )
    vault = SundaeV4Vault.model_validate(values)
    cs = SundaeV4Deployment.for_network("preview").validator("constant_sum.withdraw")
    vault.supply_module_config(cs, config)
    pools = vault.pools()
    assert all(isinstance(p, AbstractMultiAssetPoolState) for p in pools)
    return pools[0]


@pytest.fixture(autouse=True)
def _preview() -> None:
    SundaeV4Vault.select_network("preview")


def test_pools_yields_one_constant_sum_pool_on_tag_100() -> None:
    pool = _pool([(u, 1_000) for u in _units(2)], [1, 1])
    assert pool.tag == 100
    assert pool.pool_id == f"{pool.vault.pool_id}:100"
    assert pool.units() == list(pool.vault.reserves.root)
    assert pool.dex() == "SundaeSwapV4"


def test_bond_shaped_action_map_yields_two_pool_types() -> None:
    values, config = build_vault_utxo(
        [(u, 1_000) for u in _units(2)],
        prices=[1, 1],
        total_lp=2_000,
        tags={
            1: ["governance"],
            101: ["constant_sum", "fee_split"],
            102: ["constant_sum", "fee_split"],
            103: ["fairness"],
        },
    )
    vault = SundaeV4Vault.model_validate(values)
    cs = SundaeV4Deployment.for_network("preview").validator("constant_sum.withdraw")
    vault.supply_module_config(cs, config)
    assert [p.tag for p in vault.pools()] == [101, 102]
    assert len({p.pool_id for p in vault.pools()}) == 2


def test_prices_are_remapped_from_datum_order_to_units() -> None:
    u = _units(3)
    pool = _pool([(u[2], 300), (u[0], 100), (u[1], 200)], [3, 1, 2])
    assert pool.prices == {u[0]: 1, u[1]: 2, u[2]: 3}
    assert pool.price(u[0], u[2]) == (1, 3)


def test_three_asset_pool_quotes_every_ordered_pair() -> None:
    u = _units(3)
    pool = _pool([(u[0], 10_000), (u[1], 20_000), (u[2], 30_000)], [1, 2, 3])
    for a, b in itertools.permutations(u, 2):
        out, impact = pool.get_amount_out(Assets(**{a: 1_000}), b)
        assert out.unit() == b
        p_in, p_out = pool.price(a, b)
        assert out.quantity() == (1_000 * p_in - (1_000 * p_in * 3) // 1000) // p_out
        assert 0 < impact < 0.01


def test_rejects_out_unit_not_held_and_input_containing_out_unit() -> None:
    u = _units(2)
    pool = _pool([(u[0], 10), (u[1], 10)], [1, 1])
    with pytest.raises(ValueError, match="out_unit"):
        pool.get_amount_out(Assets(**{u[0]: 1}), "ff" * 28 + "ff")
    with pytest.raises(ValueError):
        pool.get_amount_out(Assets(**{u[1]: 1}), u[1])
    with pytest.raises(ValueError):
        pool.get_amount_out(Assets(**{"ff" * 28 + "ff": 1}), u[1])


def test_output_is_zero_when_the_reserve_cannot_cover_the_quote() -> None:
    u = _units(2)
    pool = _pool([(u[0], 1_000_000), (u[1], 5)], [1, 1])
    out, _ = pool.get_amount_out(Assets(**{u[0]: 100}), u[1])
    # The fee band pins the output to the exact fee-consistent quote; a
    # reserve too small to pay it admits no smaller amount either.
    assert out.quantity() == 0


def test_multi_asset_input_swap_passes_the_validator() -> None:
    u = _units(3)
    pool = _pool([(u[0], 10_000), (u[1], 20_000), (u[2], 30_000)], [1, 2, 3])
    offered = Assets(**{u[0]: 900, u[1]: 450})
    out, _ = pool.get_amount_out(offered, u[2])
    before = [pool.reserves[x] for x in pool.vault.datum_units]
    prices = [pool.prices[x] for x in pool.vault.datum_units]

    def after(amount: int) -> list[int]:
        a = list(before)
        for x, q in offered.items():
            a[pool.vault.datum_units.index(x)] += q
        a[pool.vault.datum_units.index(u[2])] -= amount
        return a

    assert check_swap(
        before, after(out.quantity()), pool.total_lp, prices, (3, 1000), (0, 1)
    )
    assert not check_swap(
        before, after(out.quantity() + 1), pool.total_lp, prices, (3, 1000), (0, 1)
    )


@pytest.mark.parametrize("n", [3, 5, 16])
@pytest.mark.parametrize("bounty", [(0, 1), (3, 2000)])
def test_quote_is_the_exact_maximum_the_validator_admits(
    n: int, bounty: tuple[int, int]
) -> None:
    u = _units(n)
    prices = [1 + (i % 4) for i in range(n)]
    reserves = [(u[i], 1_000_000 * (1 + i)) for i in range(n)]
    pool = _pool(reserves, prices, bounty_k=bounty)
    before = [pool.reserves[x] for x in pool.vault.datum_units]
    aligned_prices = [pool.prices[x] for x in pool.vault.datum_units]
    for a, b in itertools.islice(itertools.permutations(u, 2), 40):
        for amount in (1, 999, 12_345, 500_000):
            out, _ = pool.get_amount_out(Assets(**{a: amount}), b)
            after = list(before)
            after[pool.vault.datum_units.index(a)] += amount
            after[pool.vault.datum_units.index(b)] -= out.quantity()
            if out.quantity() == 0:
                # The admissible set is empty: even the smallest non-zero
                # output must also be rejected.
                probe = list(before)
                probe[pool.vault.datum_units.index(a)] += amount
                probe[pool.vault.datum_units.index(b)] -= 1
                assert not check_swap(
                    before, probe, pool.total_lp, aligned_prices, (3, 1000), bounty
                )
                continue
            assert check_swap(
                before, after, pool.total_lp, aligned_prices, (3, 1000), bounty
            ), (a, b, amount)
            after[pool.vault.datum_units.index(b)] -= 1
            assert not check_swap(
                before, after, pool.total_lp, aligned_prices, (3, 1000), bounty
            ), (a, b, amount)


@pytest.mark.parametrize("bounty", [(0, 1), (3, 2000)])
def test_small_reserve_exhaustive_admissible_set(bounty: tuple[int, int]) -> None:
    """On small reserves, exhaustively confirm the admissible set is a singleton.

    Rather than spot-checking the quote's immediate neighbours, this sweeps
    every possible output ``0..reserve`` through the oracle's ``check_swap``
    for every ordered pair and a range of input amounts: the on-chain
    predicate must admit either nothing, or exactly ``get_amount_out``'s
    quote.
    """
    u = _units(3)
    prices = [1, 2, 3]
    reserves = [(u[0], 10), (u[1], 20), (u[2], 30)]
    pool = _pool(reserves, prices, bounty_k=bounty)
    before = [pool.reserves[x] for x in pool.vault.datum_units]
    aligned_prices = [pool.prices[x] for x in pool.vault.datum_units]
    for a, b in itertools.permutations(u, 2):
        a_idx = pool.vault.datum_units.index(a)
        b_idx = pool.vault.datum_units.index(b)
        reserve_b = before[b_idx]
        for amount in range(1, 61):
            admissible = []
            for out in range(reserve_b + 1):
                after = list(before)
                after[a_idx] += amount
                after[b_idx] -= out
                if check_swap(
                    before,
                    after,
                    pool.total_lp,
                    aligned_prices,
                    (3, 1000),
                    bounty,
                ):
                    admissible.append(out)
            quote, _ = pool.get_amount_out(Assets(**{a: amount}), b)
            if quote.quantity() == 0:
                assert admissible == [], (a, b, amount, admissible)
            else:
                assert admissible == [quote.quantity()], (a, b, amount, admissible)


def test_get_amount_in_is_the_exact_minimum() -> None:
    u = _units(3)
    pool = _pool(
        [(u[0], 10_000_000), (u[1], 20_000_000), (u[2], 30_000_000)], [1, 2, 3]
    )
    for a, b in itertools.permutations(u, 2):
        for desired in (1, 7, 1_234, 99_999):
            needed, _ = pool.get_amount_in(Assets(**{b: desired}), a)
            assert (
                pool.get_amount_out(Assets(**{a: needed.quantity()}), b)[0].quantity()
                >= desired
            )
            if needed.quantity() > 1:
                assert (
                    pool.get_amount_out(Assets(**{a: needed.quantity() - 1}), b)[
                        0
                    ].quantity()
                    < desired
                )
    with pytest.raises(InvalidPoolError):
        pool.get_amount_in(Assets(**{u[0]: 10_000_001}), u[1])


def test_get_amount_in_allows_draining_the_reserve_exactly() -> None:
    """A full drain of the out reserve is admissible; one past it is not."""
    u = _units(2)
    pool = _pool([(u[0], 1_000_000), (u[1], 5_000)], [1, 1])
    needed, _ = pool.get_amount_in(Assets(**{u[1]: 5_000}), u[0])
    out, _ = pool.get_amount_out(Assets(**{u[0]: needed.quantity()}), u[1])
    assert out.quantity() == 5_000
    with pytest.raises(InvalidPoolError):
        pool.get_amount_in(Assets(**{u[1]: 5_001}), u[0])


def test_get_amount_in_raises_rather_than_loop_when_nothing_is_admissible() -> None:
    u = _units(2)
    pool = _pool([(u[0], 1_000), (u[1], 1_000)], [1000, 1])
    # The minimal fillable amount already quotes above the out reserve for
    # every larger amount too (the quote is monotone), so no input reaches
    # 999 of u1 and the search must terminate by raising, never looping.
    with pytest.raises(InvalidPoolError):
        pool.get_amount_in(Assets(**{u[1]: 999}), u[0])


def test_get_amount_in_terminates_when_bounty_dock_rejects_every_probe() -> None:
    """The search is bounded by the closed-form ceiling, not an open-ended walk.

    The price skew keeps the admissible amount range (``[1, amount_max)``) a
    few thousand wide even though the out reserve itself is ten million, so a
    naive walk bounded by the raw reserve would be far slower than this pool
    actually requires. The bounty dock rejects every amount on this
    anti-corrective direction (offering the heavily-weighted asset to drain
    the minority one), so the whole admissible range is walked and the search
    must still terminate by raising, not looping.
    """
    u = _units(2)
    pool = _pool(
        [(u[0], 10_000_000), (u[1], 10_000_000)],
        [1, 1000],
        bounty_k=(1, 2),
    )
    in_unit, out_unit = u[1], u[0]
    with pytest.raises(InvalidPoolError):
        pool.get_amount_in(Assets(**{out_unit: 1}), in_unit)


def test_apply_swap_moves_every_touched_reserve() -> None:
    u = _units(3)
    pool = _pool([(u[0], 100), (u[1], 200), (u[2], 300)], [1, 1, 1])
    pool.apply_swap(Assets(**{u[0]: 10, u[1]: 5}), Assets(**{u[2]: 14}))
    assert dict(pool.reserves.root) == {u[0]: 110, u[1]: 205, u[2]: 286}
    assert pool.vault.reserves[u[2]] == 300  # the vault snapshot is untouched


@pytest.mark.parametrize("dep", _DEPOSITS, ids=[d["scoop_tx"][:8] for d in _DEPOSITS])
def test_pinned_deposit_replays_the_real_step(dep: dict) -> None:
    """The pool-level pinned deposit reproduces a real on-chain scoop step."""
    u = _units(len(dep["reserves_before"]))
    pool = _pool(
        list(zip(u, dep["reserves_before"])),
        dep["prices"],
        total_lp=dep["lp_before"],
    )
    offered = Assets(
        **{
            x: a - b
            for x, a, b in zip(u, dep["reserves_after"], dep["reserves_before"])
        }
    )
    pinned = pool.pinned_deposit(offered)
    assert pinned.target_delta_v == dep["target_delta_v"]
    assert pinned.lp_after == dep["lp_after"]
    after = [b + d for b, d in zip(dep["reserves_before"], pinned.deltas)]
    assert after == dep["reserves_after"]


@pytest.mark.parametrize("n", [3, 5, 16])
def test_pinned_deposit_and_withdraw_pass_the_validator(n: int) -> None:
    """Both pinned steps satisfy the on-chain deposit/withdraw predicates."""
    u = _units(n)
    prices = [1 + (i % 3) for i in range(n)]
    pool = _pool([(u[i], 700_000 + 1_000 * i) for i in range(n)], prices)
    before = [pool.reserves[x] for x in pool.vault.datum_units]
    aligned = [pool.prices[x] for x in pool.vault.datum_units]

    pinned = pool.pinned_deposit(Assets(**{x: 10_000 + i for i, x in enumerate(u)}))
    after = [b + d for b, d in zip(before, pinned.deltas)]
    assert check_deposit(
        before, pool.total_lp, after, pinned.lp_after, pinned.target_delta_v, aligned
    )
    assert pinned.lp_minted == pinned.lp_after - pool.total_lp

    withdrawn = pool.pinned_withdraw(12_345)
    after_w = [b - p for b, p in zip(before, withdrawn.payouts)]
    assert check_withdraw(
        before,
        pool.total_lp,
        after_w,
        withdrawn.lp_after,
        withdrawn.target_delta_v,
        aligned,
    )
    assert withdrawn.lp_burned == pool.total_lp - withdrawn.lp_after
    assert withdrawn.lp_burned <= 12_345


def test_pinned_deposit_requires_every_reserve() -> None:
    """Leaving a reserve out of the offer is rejected, not partially filled."""
    u = _units(3)
    pool = _pool([(u[0], 100), (u[1], 100), (u[2], 100)], [1, 1, 1])
    with pytest.raises(ValueError):
        pool.pinned_deposit(Assets(**{u[0]: 10, u[1]: 10}))


def test_pinned_withdraw_rejects_more_than_the_supply() -> None:
    """Withdrawing zero or more LP than exists is rejected."""
    u = _units(2)
    pool = _pool([(u[0], 100), (u[1], 100)], [1, 1], total_lp=200)
    with pytest.raises(ValueError):
        pool.pinned_withdraw(201)
    with pytest.raises(ValueError):
        pool.pinned_withdraw(0)
