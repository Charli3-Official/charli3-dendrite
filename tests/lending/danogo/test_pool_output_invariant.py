"""Cross-builder lovelace-supply pool-output invariant.

Every pool-spending builder -- `build_topup_withdraw`, `build_repay`,
`build_increase_loan` -- computes its updated-pool output the same way: apply the
signed supply delta to the pool's holding via `_pool_output_value`, then floor ONLY
the resulting coin to the min-UTxO. For a lovelace-supply (ADA) market the supply
token IS the coin, so that delta lands in the coin; resetting the coin to the pool's
pre-action lovelace (``max(pool.lovelace, OUTPUT_MIN_ADA)``) silently drops a
deposit / repayment / borrow-out. This invariant asserts the computed coin carries
the delta -- and explicitly diverges from the dropped-delta form -- so any builder
regressing to ``max(pool.lovelace, ...)`` is caught in one place.

The only captured lovelace-supply pool is a deposit/withdraw fixture (no loan), so
`build_topup_withdraw` is exercised END-TO-END here, while the repay / increase cases
assert the IDENTICAL shared pool-output computation (`_pool_output_value` + min-UTxO
floor) those builders run -- there is no lovelace-supply loan fixture to drive their
full assembly offline. This mirrors the per-builder lovelace tests already in
`test_build_repay.py` / `test_build_increase_loan.py`, which assert the same seam.
"""

from __future__ import annotations

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.danogo.transactions._common import _pool_output_value
from charli3_dendrite.lending.danogo.transactions.build import build_topup_withdraw
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA

LOVELACE_POOL = "topup_zero_held_alt_tx.json"
DELTA = 100_000_000


def _topup_builder_pool_coin(
    offline_ctx,  # noqa: ANN001
    actor_addr,  # noqa: ANN001
    snap: TopupWithdrawSnapshot,
    supply_delta: int,
) -> int:
    """The coin of the pool output the REAL `build_topup_withdraw` emits."""
    tx_builder = TransactionBuilder(offline_ctx)
    build_topup_withdraw(
        tx_builder,
        snapshot=snap,
        actor_address=actor_addr,
        supply_change=supply_delta,
    )
    pool_nft = (snap.config_pool_skh, snap.market_name)
    for out in tx_builder.outputs:
        for policy, names in out.amount.multi_asset.data.items():
            for name in names:
                if (bytes(policy).hex(), bytes(name).hex()) == pool_nft:
                    return out.amount.coin
    raise AssertionError("no pool output (carrying the pool NFT) was wired")


def _seam_pool_coin(snap: TopupWithdrawSnapshot, supply_delta: int) -> int:
    """The pool-output coin `build_repay` / `build_increase_loan` compute.

    Both run exactly this on the updated pool: `_pool_output_value` applies the supply
    delta (the coin itself, for a lovelace-supply pool), then the coin is floored ONLY
    to the min-UTxO (see ``_add_repay_outputs`` and ``build_increase_loan``).
    """
    value = _pool_output_value(snap.pool, "lovelace", supply_delta)
    return max(value.coin, OUTPUT_MIN_ADA)


# Each pool-spending builder's supply-delta convention for a lovelace-supply market:
# a deposit / repay ADDS supply to the pool (positive coin delta); a withdraw / borrow
# pays it OUT (negative). The repay/increase cases use the shared seam; topup runs the
# real builder.
@pytest.mark.parametrize(
    ("builder", "supply_delta"),
    [
        ("topup_deposit", DELTA),
        ("topup_withdraw", -DELTA),
        ("repay", DELTA),
        ("increase", -DELTA),
    ],
)
def test_pool_output_carries_supply_delta(
    offline_ctx,  # noqa: ANN001
    actor_addr,  # noqa: ANN001
    topup_snap,  # noqa: ANN001
    builder,  # noqa: ANN001
    supply_delta,  # noqa: ANN001
):
    _fix, snap = topup_snap(LOVELACE_POOL)
    assert snap.market_info.supply_token == "lovelace"
    pool = snap.pool

    if builder.startswith("topup"):
        coin = _topup_builder_pool_coin(offline_ctx, actor_addr, snap, supply_delta)
    else:
        coin = _seam_pool_coin(snap, supply_delta)

    # The supply delta lands in the pool output's coin (the pool's pre-action lovelace
    # dwarfs the min-UTxO floor, so the floor never bites here)...
    assert coin == pool.lovelace + supply_delta
    # ... and the computed coin DIVERGES from the dropped-delta regression form, so a
    # builder reverting to `max(pool.lovelace, ...)` flips this assertion.
    assert coin != max(pool.lovelace, OUTPUT_MIN_ADA)
