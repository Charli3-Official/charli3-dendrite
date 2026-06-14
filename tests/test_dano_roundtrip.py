"""Dano serialize→re-ingest round-trip + carve byte-identity (STEEL-273).

A parsed Dano pool stores the *active* (carve-netted) reserves in ``assets``
(``post_init`` subtracts the platform fee + ADA min-utxo/swap-fee). On re-ingest
of an already-parsed representation — a dendrite ``model_dump`` OR a downstream
consumer's net projection (steelswap silver) — the carve must NOT be applied a
second time. ``skip_init`` keys on the presence of the separate ``dex_nft``
field (the "already parsed" signal) and skips ``post_init``, exactly like
VyFi / WingRiders V2.

Before this fix Dano had no ``skip_init`` and netted the carve in a *property*
on every access, so feeding back the stored (net) reserves double-subtracted —
silently, since the curve was self-consistent on the wrong reserves.

The byte-identity test pins that the refactor (which moved the carve from a
property into ``post_init``, making ``assets`` net) did not change any
externally-observable value — the curve (``get_amount_out``) and the tx-build
asset bag (``_new_pool_assets``) are identical to before, with the gross balance
reconstructed for tx-build via ``raw_x`` / ``_gross_assets``.
"""

from __future__ import annotations

import json
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.dano import DanoCLMMState

_FIX = json.loads(
    (Path(__file__).parent / "data" / "dano_pool_fixtures.json").read_text()
)


def _pool(key: str) -> DanoCLMMState:
    return DanoCLMMState.model_validate(_FIX[key]["row"])


def _net_reingest(p: DanoCLMMState) -> DanoCLMMState:
    """Reconstruct ``p`` from its NET reserves + dex_nft (the steelswap silver
    shape): the exact case that double-subtracted before ``skip_init``.
    """
    d = p._datum
    values = dict(p.model_dump())
    values["assets"] = Assets(
        root={d.unit_x: p.reserve_a, d.unit_y: p.reserve_b, "lovelace": 2_000_000},
    )
    return DanoCLMMState.model_validate(values)


def test_net_reingest_preserves_reserves() -> None:
    """STEEL-273 gate: net re-ingest must NOT re-subtract the carve."""
    # zero_reserve has a real non-zero carve (platform_fee_x = 442_697_938).
    p = _pool("zero_reserve")
    rt = _net_reingest(p)
    assert rt.reserve_a == p.reserve_a
    assert rt.reserve_b == p.reserve_b
    assert rt.virtual_reserves() == p.virtual_reserves()


def test_net_reingest_is_idempotent() -> None:
    """Re-ingesting twice is a fixed point (no progressive drift)."""
    p = _pool("zero_reserve")
    once = _net_reingest(p)
    twice = _net_reingest(once)
    assert twice.reserve_a == p.reserve_a
    assert twice.reserve_b == p.reserve_b


def test_model_dump_roundtrip() -> None:
    """The native dendrite round-trip is also idempotent for both fixtures."""
    for key in ("both_reserve", "zero_reserve"):
        p = _pool(key)
        rt = DanoCLMMState.model_validate(p.model_dump())
        assert rt.reserve_a == p.reserve_a
        assert rt.reserve_b == p.reserve_b
        assert rt.get_amount_out(Assets(root={p._datum.unit_x: 1_000_000}))[0] == (
            p.get_amount_out(Assets(root={p._datum.unit_x: 1_000_000}))[0]
        )


# Golden accessor values captured from charli3-dendrite 1.4.8 BEFORE the
# carve-in-post_init refactor. They must stay byte-identical: the curve and the
# tx-build asset bag are unchanged; only the internal ``assets`` representation
# moved from gross to net.
_GOLDEN = {
    "both_reserve": {
        "reserve_a": 100000000,
        "reserve_b": 13139014,
        "raw_x": 100000000,
        "raw_y": 13139014,
        "al0": [100000000, 13139014],
        "al1m": [101000000, 13139014],
        "gao_1m": 98476,
        "pool_change_1m": [1000000, -98476],
        "new_pool_assets": {
            "0691b2fecca1ac4f53cb6dfb00b7013e561d1f34403b957cbb5af1fa4e49474854": 13040538,
            "29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c64d494e": 101000000,
            "d8b69fc53637bcfadbc4469083f706bc293f4d9d2296646c5ca167bb5edd380dd6e1ae5469b984e6624beca0bc329964f7fcc55d5d55a22770198cf2": 1,
            "lovelace": 4500000,
        },
    },
    "zero_reserve": {
        "reserve_a": 598968631838,
        "reserve_b": 0,
        "raw_x": 599411329776,
        "raw_y": 16798851,
        "al0": [598968631838, 0],
        "al1m": [598969631838, 0],
        "gao_1m": 0,
        "pool_change_1m": [1000000, 0],
        "new_pool_assets": {
            "0691b2fecca1ac4f53cb6dfb00b7013e561d1f34403b957cbb5af1fa4e49474854": 599412329776,
            "1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378": 16798851,
            "d8b69fc53637bcfadbc4469083f706bc293f4d9d2296646c5ca167bb24d02ee9fd086a85946189232af0748c2cda616125da11679f1c2ef594966ad2": 1,
            "lovelace": 42500000,
        },
    },
}


def test_carve_byte_identity_with_pre_refactor() -> None:
    """Every externally-observable value matches pre-refactor 1.4.8 exactly."""
    for key, g in _GOLDEN.items():
        p = _pool(key)
        d = p._datum
        assert p.reserve_a == g["reserve_a"], key
        assert p.reserve_b == g["reserve_b"], key
        assert p.raw_x == g["raw_x"], key
        assert p.raw_y == g["raw_y"], key
        assert list(p.active_liquidity(0)) == g["al0"], key
        assert list(p.active_liquidity(1_000_000)) == g["al1m"], key
        out, _ = p.get_amount_out(Assets(root={d.unit_x: 1_000_000}))
        assert out.quantity() == g["gao_1m"], key
        pcx, pcy = p.compute_pool_change(1_000_000, 0)
        assert [pcx, pcy] == g["pool_change_1m"], key
        npa = p._new_pool_assets(pcx, pcy, swap_fee=1_500_000, staking_reward=0)
        assert dict(npa.root) == g["new_pool_assets"], key
