"""CSWAP pricing: LP fee in the curve, platform fee on the ADA leg."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.cswap import CSwapCPPState
from charli3_dendrite.dexs.amm.cswap import CSwapPoolDatum

_FILLS = json.loads(
    (Path(__file__).parent / "data" / "cswap_swap_fills.json").read_text(),
)["fills"]

_POLICY = "ab" * 28
_TOKEN = _POLICY + "746f6b656e"
_POOL_NFT = _POLICY + "63"
_BASIS = 10000


def _state(ada: int, token: int, pool_fee: int) -> CSwapCPPState:
    """A pool already decoded (reserves net of the 2 ADA deposit) at ``pool_fee``."""
    return CSwapCPPState.model_validate(
        {
            "address": CSwapCPPState.pool_selector().addresses[0],
            "assets": {"lovelace": ada, _TOKEN: token},
            "pool_nft": {_POOL_NFT: 1},
            "fee": pool_fee,
            "block_time": 0,
            "block_index": 0,
            "plutus_v2": False,
            "tx_index": 0,
            "tx_hash": "00" * 32,
            "datum_cbor": "",
            "datum_hash": "",
        },
    )


def _units(ada_in: bool) -> tuple[str, str]:
    return ("lovelace", _TOKEN) if ada_in else (_TOKEN, "lovelace")


def _pool(fill: dict) -> tuple[CSwapCPPState, str, str]:
    """The fill's before-state pool and its (unit in, unit out)."""
    ada_in = fill["direction"] == "ada_in"
    ada, token = fill["reserve_in"], fill["reserve_out"]
    if not ada_in:
        ada, token = token, ada
    return (_state(ada, token, fill["pool_fee"]), *_units(ada_in))


def _out(pool: CSwapCPPState, unit_in: str, amount: int) -> int:
    return pool.get_amount_out(Assets(**{unit_in: amount}))[0].quantity()


def _floor_platform_fee_out(fill: dict) -> int:
    """The payout if the platform fee were rounded down instead of up."""
    m = _BASIS - fill["pool_fee"]
    x, r_in, r_out = fill["amount_in"], fill["reserve_in"], fill["reserve_out"]
    if fill["direction"] == "ada_in":
        x -= x * 15 // _BASIS
        return x * m * r_out // (x * m + r_in * _BASIS)
    gross = x * m * r_out // (x * m + r_in * _BASIS)
    return gross - gross * 15 // _BASIS


def test_fixture_is_non_trivial() -> None:
    """Both directions, many pool fees, and fills that depend on rounding up."""
    assert len(_FILLS) >= 40
    assert {f["direction"] for f in _FILLS} == {"ada_in", "ada_out"}
    assert len({f["pool_fee"] for f in _FILLS}) >= 5
    assert any(_floor_platform_fee_out(f) != f["amount_received"] for f in _FILLS)


@pytest.mark.parametrize("fill", _FILLS, ids=lambda f: f["tx_hash"][:16])
def test_get_amount_out_reproduces_the_real_payout(fill: dict) -> None:
    pool, unit_in, unit_out = _pool(fill)

    amount_out, _ = pool.get_amount_out(Assets(**{unit_in: fill["amount_in"]}))

    assert amount_out.unit() == unit_out
    assert amount_out.quantity() == fill["amount_received"]


@pytest.mark.parametrize("fill", _FILLS, ids=lambda f: f["tx_hash"][:16])
def test_get_amount_in_is_minimal(fill: dict) -> None:
    pool, unit_in, unit_out = _pool(fill)
    want = fill["amount_received"]

    amount_in, _ = pool.get_amount_in(Assets(**{unit_out: want}))
    need = amount_in.quantity()

    assert amount_in.unit() == unit_in
    assert need <= fill["amount_in"]
    assert _out(pool, unit_in, need) >= want
    assert _out(pool, unit_in, need - 1) < want


def test_post_init_fee_is_the_datum_pool_fee() -> None:
    """A decoded pool prices at its LP fee; the platform fee stays separate."""
    datum = CSwapPoolDatum(
        total_lp_tokens=1_000_000,
        pool_fee=35,
        quote_policy=b"",
        quote_name=b"",
        base_policy=bytes.fromhex(_POLICY),
        base_name=bytes.fromhex(_TOKEN[56:]),
        lp_token_policy=bytes.fromhex("cd" * 28),
        lp_token_name=b"lp",
    )
    pool = CSwapCPPState.model_validate(
        {
            "address": CSwapCPPState.pool_selector().addresses[0],
            "assets": {
                "lovelace": 502_000_000,
                _TOKEN: 7_000_000,
                _POOL_NFT: 1,
            },
            "block_time": 0,
            "block_index": 0,
            "plutus_v2": False,
            "tx_index": 0,
            "tx_hash": "00" * 32,
            "datum_cbor": datum.to_cbor_hex(),
            "datum_hash": "",
        },
    )

    assert pool.fee == 35
    assert pool.volume_fee == 35
    assert pool.platform_fee == 15
    assert pool.reserve_a == 500_000_000
    assert pool.reserve_b == 7_000_000


def test_swap_amounts_legs() -> None:
    """ADA in: the pool is credited the input net of the fee. ADA out: the
    pool pays the gross curve output and the swapper gets it net of the fee."""
    pool = _state(1_000_000_000, 50_000_000, 85)

    pool_in, pool_out, out = pool.swap_amounts("lovelace", 10_000_000)
    assert pool_in == 10_000_000 - 15_000
    assert out == pool_out == _out(pool, "lovelace", 10_000_000)
    assert pool_out == pool_in * 9915 * 50_000_000 // (
        pool_in * 9915 + 1_000_000_000 * _BASIS
    )

    pool_in, pool_out, out = pool.swap_amounts(_TOKEN, 1_000_000)
    assert pool_in == 1_000_000
    assert pool_out == 1_000_000 * 9915 * 1_000_000_000 // (
        1_000_000 * 9915 + 50_000_000 * _BASIS
    )
    assert out == pool_out - -(-pool_out * 15 // _BASIS)
    assert out == _out(pool, _TOKEN, 1_000_000)


@pytest.mark.parametrize(
    ("amount", "fee"),
    [(1, 1), (666, 1), (667, 2), (6667, 11), (10_000, 15)],
)
def test_platform_fee_rounds_up(amount: int, fee: int) -> None:
    pool = _state(10**12, 10**12, 0)

    pool_in, _, _ = pool.swap_amounts("lovelace", amount)

    assert amount - pool_in == fee


def test_one_lovelace_buys_nothing() -> None:
    pool = _state(10**12, 10**12, 0)

    assert pool.swap_amounts("lovelace", 1) == (0, 0, 0)
    assert _out(pool, "lovelace", 1) == 0


def test_platform_fee_is_not_priced_as_a_curve_fee() -> None:
    """A small swap keeps ~(1 - 0.0085)(1 - 0.0015) of its value, more than the
    (1 - 0.0100) a curve priced at ``pool_fee + 15`` would leave."""
    pool = _state(10**12, 10**12, 85)
    legacy = 1_000_000 * 9900 * 10**12 // (1_000_000 * 9900 + 10**12 * _BASIS)

    assert legacy == 989_999
    assert _out(pool, "lovelace", 1_000_000) == 990_011
    assert _out(pool, _TOKEN, 1_000_000) == 990_011


@pytest.mark.parametrize("pool_fee", [0, 30, 85, 285])
def test_get_amount_in_minimal_property(pool_fee: int) -> None:
    """``out(need - 1) < want <= out(need)`` on random small pools."""
    rng = random.Random(pool_fee)
    for _ in range(400):
        pool = _state(
            rng.randint(10**3, 10**8), rng.randint(10**3, 10**8), pool_fee
        )
        ada_in = rng.random() < 0.5
        unit_in, unit_out = _units(ada_in)
        reserve_out = pool.reserve_b if ada_in else pool.reserve_a
        want = rng.randint(1, reserve_out * 9 // 10)

        need = pool.get_amount_in(Assets(**{unit_out: want}))[0].quantity()

        assert _out(pool, unit_in, need) >= want
        assert _out(pool, unit_in, need - 1) < want
