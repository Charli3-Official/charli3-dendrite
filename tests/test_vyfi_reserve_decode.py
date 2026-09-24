"""VyFi post_init subtracts each fee from the reserve matching VyFi's own
a_asset / b_asset unit, not the canonical sort position."""

import types

import pytest
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.vyfi import VyFiCPPState, VyFiPoolDatum
from charli3_dendrite.dexs.core.errors import NotAPoolError

NFT = "cc" * 28
POL_X, POL_Y = "aa" * 28, "bb" * 28
UNIT_X = POL_X + "01"  # canonically-smaller token
UNIT_Y = POL_Y + "02"  # canonically-larger token


def _asset(cs, tn):
    return types.SimpleNamespace(currency_symbol=cs, token_name=tn)


def _pooldef(a_cs, a_tn, b_cs, b_tn):
    # duck-typed VyFiPoolDefinition: post_init only reads .json_.a_asset/.b_asset
    return types.SimpleNamespace(
        json_=types.SimpleNamespace(
            a_asset=_asset(a_cs, a_tn), b_asset=_asset(b_cs, b_tn)
        )
    )


def _mock_pools(monkeypatch, pooldef):
    monkeypatch.setattr(VyFiCPPState, "pools", classmethod(lambda cls: {NFT: pooldef}))


def _values(reserves, token_a_fees, token_b_fees):
    datum = VyFiPoolDatum(
        token_a_fees=token_a_fees, token_b_fees=token_b_fees, lp_tokens=0
    )
    return {
        "assets": Assets(root=dict(reserves)),
        "datum_cbor": datum.to_cbor(),
        "pool_nft": Assets(root={NFT: 1}),
    }


def test_reversed_pool_fee_lands_on_vyfi_a_asset(monkeypatch):
    # VyFi lists the canonically-LARGER token (UNIT_Y) as a_asset -> reversed.
    _mock_pools(monkeypatch, _pooldef(POL_Y, "0x02", POL_X, "0x01"))
    values = _values(
        {UNIT_X: 263_770, UNIT_Y: 21_104_082_844, "lovelace": 2_000_000},
        token_a_fees=1_520_000,  # belongs to UNIT_Y (VyFi a)
        token_b_fees=139,  # belongs to UNIT_X (VyFi b)
    )
    VyFiCPPState.post_init(values)
    a = values["assets"]
    assert a[UNIT_Y] == 21_104_082_844 - 1_520_000
    assert a[UNIT_X] == 263_770 - 139
    assert a[UNIT_X] > 0 and a[UNIT_Y] > 0
    assert a["lovelace"] == 2_000_000


def test_aligned_token_pool_unchanged(monkeypatch):
    # VyFi a_asset == canonically-smaller (UNIT_X): fee mapping equals the old behavior.
    _mock_pools(monkeypatch, _pooldef(POL_X, "0x01", POL_Y, "0x02"))
    values = _values(
        {UNIT_X: 5_000_000, UNIT_Y: 9_000_000, "lovelace": 2_000_000},
        token_a_fees=1_000,  # UNIT_X (VyFi a)
        token_b_fees=2_000,  # UNIT_Y (VyFi b)
    )
    VyFiCPPState.post_init(values)
    a = values["assets"]
    assert a[UNIT_X] == 5_000_000 - 1_000
    assert a[UNIT_Y] == 9_000_000 - 2_000
    assert a["lovelace"] == 2_000_000


def test_ada_pool_fee_and_min_utxo(monkeypatch):
    TOKEN = POL_X + "aa"
    # VyFi a_asset == ADA (cs="", tn=""), b_asset == TOKEN.
    _mock_pools(monkeypatch, _pooldef("", "", POL_X, "0xaa"))
    values = _values(
        {"lovelace": 10_000_000, TOKEN: 4_000_000},
        token_a_fees=500_000,  # ADA side
        token_b_fees=1_234,  # TOKEN side
    )
    VyFiCPPState.post_init(values)
    a = values["assets"]
    assert a["lovelace"] == 10_000_000 - 2_000_000 - 500_000
    assert a[TOKEN] == 4_000_000 - 1_234


def test_residual_negative_raises_typed_error(monkeypatch):
    # Even correctly mapped, a fee larger than its reserve must not store a negative.
    _mock_pools(monkeypatch, _pooldef(POL_X, "0x01", POL_Y, "0x02"))
    values = _values(
        {UNIT_X: 100, UNIT_Y: 9_000_000, "lovelace": 2_000_000},
        token_a_fees=5_000,  # exceeds UNIT_X reserve (100)
        token_b_fees=1,
    )
    with pytest.raises(NotAPoolError):
        VyFiCPPState.post_init(values)
