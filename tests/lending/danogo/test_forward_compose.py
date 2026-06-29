"""The analytics resolver prices cross-quote markets through the ADA intermediate.

`oracles.forward.resolve_prices` (the read-only path `loader.snapshot` drives) must
price collateral in the market's actual supply-token quote, composing through ADA for
collateral whose pool prices it in ADA while the market quote is a different token --
exactly as the transaction builder does. The partial-repay fixture is a JedMicroUSD
market locking a Danogo pool dtoken: the on-chain oracle redeemer declares the
intermediate ``ada -> JedMicroUSD`` price AND the composed ``dtoken -> JedMicroUSD``
price. The analytics path must reproduce that map byte-exact (not a lovelace-basis
price and not a single standalone dtoken price missing the intermediate).
"""

from types import SimpleNamespace
from typing import Callable

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.danogo.oracles.forward import resolve_prices
from charli3_dendrite.lending.danogo.oracles.locator import load_registry
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.transactions._common import (
    _forward_prices_and_leaves,
)
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.danogo.transactions.context import _loan_collateral_units

PARTIAL = "decrease_loan_partial_tx.json"


class _LeafBackend:
    """Serves a snapshot's source leaves by address (with optional asset filter).

    Mirrors the live backend seam `resolve_leaf_info` reads: each leaf is addressed by
    its UTxO address, carries its inline datum, and its asset bag includes the lovelace
    balance so the leaf parsers see the same value the on-chain UTxO holds.
    """

    def __init__(self, leaves):
        self._by_addr = {}
        for u in leaves:
            root = {}
            if u.lovelace:
                root["lovelace"] = u.lovelace
            for policy, name, qty in u.assets:
                unit = "lovelace" if not policy else policy + name
                root[unit] = root.get(unit, 0) + int(qty)
            self._by_addr.setdefault(u.address, []).append(
                SimpleNamespace(
                    address=u.address,
                    datum_cbor=u.datum,
                    assets=Assets(root=root),
                )
            )

    def get_pool_utxos(
        self, addresses, assets=None, limit=1000, page=0, historical=True
    ):
        out = []
        for addr in addresses:
            for info in self._by_addr.get(addr, []):
                if assets and not all(a in info.assets.root for a in assets):
                    continue
                out.append(info)
        return out


def test_analytics_prices_cross_quote_market_byte_exact(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    fix, snapshot = repay_snap(PARTIAL)
    quote = snapshot.market_info.supply_token
    captured = OraclePriceCalcRdmr.from_cbor(fix["oracle_redeemer"])

    collateral_units = _loan_collateral_units(
        snapshot.loan,
        loan_skh=snapshot.loan_skh,
        market_name=snapshot.market_name,
    )
    # The transaction builder is the proven-correct reference (its result is byte-exact
    # to the on-chain redeemer); the analytics path must reproduce the same prices.
    tx_prices, _ = _forward_prices_and_leaves(snapshot, set(collateral_units))

    backend = _LeafBackend(snapshot.oracle_source_leaves)
    pm = resolve_prices(
        backend,
        registry=load_registry(),
        pairs=[(unit, quote) for unit in collateral_units],
    )

    # Every entry the tx builder / on-chain redeemer declares under this quote -- the
    # intermediate ada price and the composed collateral price -- is reproduced exactly.
    assert tx_prices[quote] == captured.prices[quote]
    assert set(pm.prices) == set(captured.prices[quote])
    for token, (num, denom) in captured.prices[quote].items():
        price = pm.get(token)
        assert price is not None
        assert price.quote == quote
        assert (price.num, price.denom) == (num, denom)
