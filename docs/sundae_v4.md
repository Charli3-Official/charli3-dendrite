# SundaeSwap V4

A V4 pool UTxO is an N-asset **vault**. `SundaeV4Vault` parses one from its
value and inline datum: the pool NFT and LP token (CIP-68 names under the
pool-mint policy), the N declared reserves, the action map, the module
commitments and the lovelace surplus. It prices nothing.

A **pool type** is a module bound to the vault on an action tag.
`vault.pools()` returns one pool type per enabled invariant-module binding — a
`SundaeV4ConstantSumPool` for a constant-sum binding, a `SundaeV4StableSwapPool`
for a stableswap one; each quotes with explicit units:

```python
from charli3_dendrite import SundaeV4Vault
from charli3_dendrite.dataclasses.models import Assets

vault = SundaeV4Vault.model_validate(utxo.model_dump())   # a PoolStateInfo
pool = vault.pools()[0]
out, impact = pool.get_amount_out(Assets(**{unit_in: 1_000_000}), unit_out)
```

V4 is deployed on `mainnet`, `preprod` and `preview`; the class family targets
`mainnet` by default, and `SundaeV4Vault.select_network("preview")` points it at
a testnet deployment the same way `"preprod"` does.

Module configs are committed by hash in the datum. They resolve from a
caller-supplied preimage (`module_configs=` at construction or
`supply_module_config`), a cache keyed by the commitment, or the last
transaction that ran the module through `backend.get_redeemers` (db-sync and
Blockfrost) — usually the producing transaction, else found by walking the
pool NFT's UTxO history via `backend.get_pool_utxos`. Every resolved config is
hash-verified.

## Stableswap pools

A stableswap vault binds the `stableswap` module (`vault.invariant_modules()`
lists it) and prices its two reserves on Curve's invariant
`4A(x + y) + D = 4AD + D^3 / (4xy)`, each reserve scaled by its config rate and
`10^12`. The validator admits exactly one output per input, and
`SundaeV4StableSwapPool` computes exactly that integer: `get_amount_out` is the
output an order receives, `get_amount_in` the exact minimum input for an output,
`max_output` the smallest output no ledger-sized offer reaches, and
`sum_invariant` the invariant `D` the next step must declare. `price` is the
marginal rate as integer weights, and `pinned_deposit` / `pinned_withdraw` are the
proportional liquidity moves, with `D` as the measure. The math is the contract's
own integer reference (`charli3_dendrite.dexs.amm.sundae_v4_stableswap_math`).

A stableswap config's rates can change between scoops: a rate update, signed by
the config's `rate_manager`, commits the config with the new rates while the
scoop's `Operate` redeemer still carries the old one. Config resolution also tries
each recorded config with the rates of every rate update in the same transaction,
accepting only a hash match. A quote is priced at the vault's current rates; a
scoop that opens with a rate update prices an order at the new ones, so its
minimum received should allow for `max_rate_step` (uncapped when `None`).

## Multi-vault routes

A basic swap names only what it offers and the least it must receive, so the
scooper may route it across several vaults (for example USDCx → USDr on a
constant-sum vault, then USDr → sUSDr on a stableswap vault). It does so when the
order's fee budget pays for the route:

```
route_fee_budget(pools, hops) = base_fee + (pools − 1) · 1 ADA + (hops − 1) · 0.5 ADA
```

Build such an order with `swap_utxo(..., fee_budget=SundaeV4Vault.route_fee_budget(k, k))`
on any bound pool, for a straight path over `k` vaults: the datum's `service_budget` and
`max_per_execution` are both the budget, and the order locks it with the 2 ADA rider.

## Forwarding

A V4 order pays a fixed destination: an address and, optionally, a datum carried
inline. Pointing the destination at another protocol's order address, with that
order's datum inline, forwards the fill into it (`swap_forward` is `True` on the V4
pool types). Everything in the order but the consumed offer and the fee leaves with
the output, so the next order's batcher fee and deposit ride as the order's rider:

```python
pool.swap_utxo(
    address_source,
    in_assets,
    min_received,
    address_target=next_order_address,
    datum_target=next_order_datum,
    rider=next_fee + next_deposit,
)
```

`rider` defaults to the 2 ADA a payout returns and may not be lower. It changes the
order's value only, never its datum.

::: charli3_dendrite.dexs.amm.sundae_v4.SundaeV4Vault

::: charli3_dendrite.dexs.amm.sundae_v4.SundaeV4ConstantSumPool

::: charli3_dendrite.dexs.amm.sundae_v4.SundaeV4StableSwapPool

::: charli3_dendrite.dexs.amm.multi_asset.AbstractMultiAssetPoolState
