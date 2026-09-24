"""Build synthetic SundaeSwap V4 vault UTxOs for tests.

The datum is a real :class:`SundaeV4PoolDatum` with an N-entry reserve
declaration, an action map in the deployed constant-sum package shape and a
``module_state`` whose constant-sum slot commits to the returned config; the
value bag carries the reserves, the pool NFT, the preminted LP and a lovelace
surplus, exactly as a live vault does.
"""

from __future__ import annotations

import hashlib

from pycardano import IndefiniteList
from pycardano import RawPlutusData
from pycardano.serialization import CBORTag

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dexs.amm.sundae_v4 import ActionEntry
from charli3_dendrite.dexs.amm.sundae_v4 import BoolTrue
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumConfig
from charli3_dendrite.dexs.amm.sundae_v4 import Rational
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import module_config_hash

CONFIG_LESS = b"\x80"


def _asset_class(unit: str) -> AssetClass:
    """The on-chain asset class of a dendrite unit (``lovelace`` is empty/empty)."""
    if unit == "lovelace":
        return AssetClass(policy=b"", asset_name=b"")
    return AssetClass(
        policy=bytes.fromhex(unit[:56]), asset_name=bytes.fromhex(unit[56:])
    )


def build_vault_utxo(
    reserves: list[tuple[str, int]],
    *,
    prices: list[int],
    fee: tuple[int, int] = (3, 1000),
    bounty_k: tuple[int, int] = (0, 1),
    balance_fee: tuple[int, int] = (0, 1),
    total_lp: int,
    identifier: bytes = b"\x11" * 28,
    surplus: int = 3_000_000,
    tags: dict[int, list[str]] | None = None,
    network: str = "preview",
) -> tuple[dict, ConstantSumConfig]:
    """A vault UTxO ``values`` dict plus the constant-sum config it commits to.

    ``reserves`` is in datum (declaration) order; ``prices`` is aligned to it.
    ``tags`` maps action tag -> module kinds; default is the deployed
    constant-sum package ``{100: [constant_sum, fee_split, fairness],
    200: [treasury_policy], 1: [governance]}``.
    """
    deployment = SundaeV4Deployment.for_network(network)
    if len(prices) != len(reserves):
        msg = "prices must align with reserves"
        raise ValueError(msg)
    config = ConstantSumConfig(
        prices=IndefiniteList(list(prices)),
        fee=Rational(num=fee[0], den=fee[1]),
        bounty_k=Rational(num=bounty_k[0], den=bounty_k[1]),
        balance_fee=Rational(num=balance_fee[0], den=balance_fee[1]),
    )
    tags = tags or {
        100: ["constant_sum", "fee_split", "fairness"],
        200: ["treasury_policy"],
        1: ["governance"],
    }
    actions = [
        ActionEntry(
            tag=tag,
            enabled=BoolTrue(),
            modules=IndefiniteList(
                [deployment.validator(f"{kind}.withdraw") for kind in kinds],
            ),
        )
        for tag, kinds in tags.items()
    ]
    cs_hash = deployment.validator("constant_sum.withdraw")
    module_state: list = []
    seen: set[bytes] = set()
    for _tag, kinds in tags.items():
        for kind in kinds:
            h = deployment.validator(f"{kind}.withdraw")
            if h in seen:
                continue
            seen.add(h)
            if h == cs_hash:
                commitment = module_config_hash(config)
            elif kind == "fairness":
                commitment = CONFIG_LESS
            else:
                # An opaque committed config for modules the tests never resolve.
                commitment = hashlib.blake2b(kind.encode(), digest_size=32).digest()
            module_state.append(IndefiniteList([h, commitment]))
    datum = SundaeV4PoolDatum(
        assets=IndefiniteList(
            [IndefiniteList([_asset_class(u), q]) for u, q in reserves],
        ),
        total_lp=total_lp,
        circulating_lp=total_lp,
        preminted_lp=0,
        identifier=identifier,
        actions=actions,
        module_state=IndefiniteList(module_state),
        min_surplus=2_000_000,
        extension=RawPlutusData(CBORTag(121, [])),
    )
    policy = deployment.pool_nft_policy.hex()
    value = {u: q for u, q in reserves}
    value["lovelace"] = value.get("lovelace", 0) + surplus
    value[policy + "000de140" + identifier.hex()] = 1
    value[policy + "0014df10" + identifier.hex()] = 1  # one LP token held as a rider
    values = {
        "tx_hash": "ab" * 32,
        "tx_index": 0,
        "datum_cbor": datum.to_cbor().hex(),
        "datum_hash": "cd" * 32,
        "assets": value,
        "block_time": 0,
        "block_index": 0,
    }
    return values, config
