# SundaeSwap V4

A V4 pool UTxO is an N-asset **vault**. `SundaeV4Vault` parses one from its
value and inline datum: the pool NFT and LP token (CIP-68 names under the
pool-mint policy), the N declared reserves, the action map, the module
commitments and the lovelace surplus. It prices nothing.

A **pool type** is a module bound to the vault on an action tag.
`vault.pools()` returns one `SundaeV4ConstantSumPool` per enabled
constant-sum binding; each quotes with explicit units:

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

::: charli3_dendrite.dexs.amm.sundae_v4.SundaeV4Vault

::: charli3_dendrite.dexs.amm.sundae_v4.SundaeV4ConstantSumPool

::: charli3_dendrite.dexs.amm.multi_asset.AbstractMultiAssetPoolState
