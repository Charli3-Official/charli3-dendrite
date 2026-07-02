"""SSPoolRedeemer.set_idx must set ``pool_in_idx`` (not a nonexistent ``self_index``).

The stable-swap redeemer carries ``pool_in_idx``/``pool_out_idx`` (unlike the CPP
redeemer's ``self_index``); a copy-paste left ``set_idx`` assigning ``self.self_index``,
so ``pool_in_idx`` stayed 0 and the on-chain redeemer pointed at the wrong input.
"""

from pycardano import Address, Network, TransactionOutput, VerificationKeyHash

from charli3_dendrite.dexs.amm.splash import SSPoolRedeemer, SwapAction

# The stable-swap pool contract address ``set_idx`` matches the pool output against.
_POOL_ADDR = "addr1w9wnm7vle7al9q4aw63aw63wxz7aytnpc4h3gcjy0yufxwc3mr3e5"


class _Key:
    def __init__(self, index: int) -> None:
        self.index = index


class _Val:
    def __init__(self, data: object) -> None:
        self.data = data


class _FakeBuilder:
    """Minimal ``TransactionBuilder`` stand-in: one redeemer keyed at input index 2."""

    def __init__(self, redeemer: object, outputs: list) -> None:
        self._redeemers = {_Key(2): _Val(redeemer)}
        self.outputs = outputs

    def redeemers(self):
        return self._redeemers


def test_ssp_set_idx_sets_pool_in_idx() -> None:
    red = SSPoolRedeemer(
        pool_in_idx=0, pool_out_idx=0, action=SwapAction(context_values_list=[0])
    )
    non_pool = TransactionOutput(
        Address(VerificationKeyHash(bytes(28)), network=Network.MAINNET), 2_000_000
    )
    pool_out = TransactionOutput(Address.from_primitive(_POOL_ADDR), 2_000_000)

    red.set_idx(_FakeBuilder(red, [non_pool, pool_out]))

    # pool_in_idx comes from the matched redeemer key (was left 0 by the self_index bug).
    assert red.pool_in_idx == 2
    # pool_out_idx is the pool output's position (index 1 here).
    assert red.pool_out_idx == 1
