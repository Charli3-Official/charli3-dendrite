"""Splash swap_utxo preserves a token/token pool's min-UTxO ADA.

A Splash pool whose two reserves are both native tokens still holds lovelace as min-UTxO.
The reserve-bearing ``self.assets`` carries only the two token reserves (a lovelace key
would sort to index 0 and corrupt the positional reserve_a/reserve_b indexing), so a pool
value rebuilt from it drops that ADA -- ``asset_to_value`` emits coin 0 and the builder
defaults the recreated pool output to min-ADA. The pool validator requires the recreated
pool output to preserve the pool's lovelace exactly, so ``swap_utxo`` reinstates it from the
resolved on-chain pool UTxO. For an ADA-paired pool lovelace is a genuine reserve (carried
and updated by the swap), so the reinstatement is a no-op.
"""

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.splash import SplashBaseState

_T1 = "aa" + "11" * 27  # native-token units (policy+name hex)
_T2 = "bb" + "22" * 27


class _Pool:
    """Minimal stand-in exposing only what the helper reads: unit_a / unit_b."""

    def __init__(self, unit_a: str, unit_b: str) -> None:
        self.unit_a = unit_a
        self.unit_b = unit_b


def test_reinstate_lovelace_token_token_pool() -> None:
    # The rebuilt value has NO lovelace (both reserves are tokens); the on-chain pool UTxO
    # holds 10 ADA min-UTxO -> it must be reinstated, else the output defaults to min-ADA.
    rebuilt = Assets(root={_T1: 100, _T2: 200})
    on_chain = Assets(root={"lovelace": 10_000_000, _T1: 100, _T2: 200})
    SplashBaseState._reinstate_pool_lovelace(_Pool(_T1, _T2), rebuilt, on_chain)
    assert rebuilt["lovelace"] == 10_000_000


def test_reinstate_lovelace_ada_pool_is_noop() -> None:
    # ADA-paired pool: lovelace is a real reserve, already carried and swap-updated. The
    # helper must NOT overwrite it with the (pre-swap) on-chain coin.
    rebuilt = Assets(root={"lovelace": 55, _T2: 200})
    on_chain = Assets(root={"lovelace": 10_000_000, _T2: 200})
    SplashBaseState._reinstate_pool_lovelace(_Pool("lovelace", _T2), rebuilt, on_chain)
    assert rebuilt["lovelace"] == 55
