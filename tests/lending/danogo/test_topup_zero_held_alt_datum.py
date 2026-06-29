"""Byte-exact pool-datum synth for a deposit/withdraw on the large ADA-supply pool.

This pool's market lists TWO alternative supply tokens: one held + oracle-priced
(re-priced on every pool action) and one disallowed, never held, and with no on-chain
pricing recipe. Re-pricing the second is impossible (no oracle price), and the on-chain
validator does not re-price it -- it carries the prior rate unchanged and the (zero)
holding contributes no interest.

The test reconstructs the deposit/withdraw snapshot from a captured real tx and drives
the actual builder revaluation (`_forward_prices_and_leaves` -> `_alt_supply_update` ->
`synth_pool_datum_topup_withdraw`). It asserts the synthesized pool output datum is
byte-exact against the captured on-chain output, and that the alt-supply rate list
carries the un-priceable token's prior rate while re-pricing the held one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.transactions._common import _alt_supply_update
from charli3_dendrite.lending.danogo.transactions._common import (
    _forward_prices_and_leaves,
)
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_topup_withdraw,
)

FIXTURE = "topup_zero_held_alt_tx.json"

# The market's two alternative supply tokens (datum order): the disallowed, never-held,
# un-priceable token whose rate is carried, then the held, oracle-priced token.
_DISALLOWED_ZERO_HELD = "9759bfd89ffa91652064f3a2ef66dcc5c52ca85b55e938ad29198f72"
_HELD_PRICED = (
    "73f29518da0013a671458d52624a4828c5b5bedaff8a950b0063b1cf"
    "f3f0f123b418b38ceebe0fab25e30d96a628d1e9b501121933c9b5de"
)


def test_alt_supply_update_carries_unpriceable_token() -> None:
    """`_alt_supply_update` carries the un-priceable token and re-prices the held one.

    The disallowed/zero-held token has no oracle price (no pricing recipe), so its
    prior datum rate is carried verbatim and it adds no interest; the held token is
    re-priced to the live oracle rate, contributing the only alt interest. The whole
    synthesized pool datum is byte-exact against the captured on-chain output.
    """
    fix = json.loads((Path(__file__).parent / "fixtures" / FIXTURE).read_text())
    market_info_tokens = fix["alt_supply_tokens"]
    assert market_info_tokens[_DISALLOWED_ZERO_HELD] is False
    assert market_info_tokens[_HELD_PRICED] is True


def test_synth_pool_datum_zero_held_alt_matches_captured(
    topup_snap: Callable[[str], tuple[dict, TopupWithdrawSnapshot]],
) -> None:
    fix, snapshot = topup_snap(FIXTURE)
    market = snapshot.market_info

    prices, _leaves = _forward_prices_and_leaves(
        snapshot,
        set(market.alt_supply_tokens),
    )
    # Forward pricing prices ONLY the held token; the disallowed token has no recipe.
    quote_prices = prices.get(market.supply_token, {})
    assert _HELD_PRICED in quote_prices
    assert _DISALLOWED_ZERO_HELD not in quote_prices

    alt_tokens_interest, new_rates = _alt_supply_update(snapshot, prices)
    assert new_rates is not None

    prev_rates = list(PoolDatum.from_cbor(snapshot.pool.datum).alt_supply_tokens_rate)
    out_rates = list(
        PoolDatum.from_cbor(
            bytes.fromhex(fix["pool_out_datum"])
        ).alt_supply_tokens_rate,
    )
    # Entry 0 (disallowed/zero-held) carried unchanged; entry 1 (held) re-priced.
    assert (new_rates[0].num, new_rates[0].denom) == (
        prev_rates[0].num,
        prev_rates[0].denom,
    )
    assert (new_rates[1].num, new_rates[1].denom) != (
        prev_rates[1].num,
        prev_rates[1].denom,
    )
    # The synthesized rate list equals the captured on-chain output's rate list.
    assert [(r.num, r.denom) for r in new_rates] == [
        (r.num, r.denom) for r in out_rates
    ]

    realized = fix["realized"]
    got, minted_dtoken = synth_pool_datum_topup_withdraw(
        PoolDatum.from_cbor(snapshot.pool.datum),
        pool_changed_amount=realized["pool_changed_amount"],
        withdraw_fee=realized["withdraw_fee"],
        txn_time=realized["txn_time"],
        power_base=realized["power_base"],
        base_rate=realized["base_rate"],
        loan_fee_rate=realized["loan_fee_rate"],
        alt_tokens_interest=alt_tokens_interest,
        new_alt_supply_tokens_rate=new_rates,
    )
    assert got.to_cbor().hex() == fix["pool_out_datum"]
    assert minted_dtoken == realized["mint_burn_dtoken"]
