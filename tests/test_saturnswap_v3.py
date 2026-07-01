"""SaturnSwap V3 (PlutusV3) decode + order-state wiring tests.

Fixtures are real mainnet V3 SwapDatum CBOR captured from the V3 order script
address and the filler-repo cited proof transactions (see
``tests/fixtures/saturnswap_v3/README``).
"""

from pathlib import Path

import pytest
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dexs.ob.saturnswap import _SATURNSWAP_ORDER_STATE_CLASSES
from charli3_dendrite.dexs.ob.saturnswap import SATURNSWAP_V3_ORDER_ADDRESS
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapOrderState
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapOutputReferenceV3
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapSomeCoverage
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapSwapDatumV3
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapV3OrderState
from pycardano import Address
from pycardano import PlutusV3Script

_FIX = Path(__file__).parent / "fixtures" / "saturnswap_v3"
_COVERED = "order_b6bcaeb6_out1_cov_some.hex"
_UNCOVERED = [
    "order_ad182bcd.hex",
    "order_18a01339.hex",
    "order_b6bcaeb6_out0_cov_none.hex",
    "order_b6bcaeb6_out2_cov_none.hex",
]
_ALL = [*_UNCOVERED, _COVERED]
_MIN_PARTIAL = {
    "order_ad182bcd.hex": 1_000_000,
    "order_18a01339.hex": 0,
    "order_b6bcaeb6_out0_cov_none.hex": 0,
    "order_b6bcaeb6_out2_cov_none.hex": 1_000_000,
    _COVERED: 0,
}
_COVERED_PREMIUM_BPS = 100
_PREMIUM_ON_1M = 10_000
_V3_TAKER_FEE_BPS = 100


def _hex(name: str) -> str:
    return (_FIX / name).read_text().strip()


@pytest.mark.parametrize("name", _ALL)
def test_v3_datum_roundtrip(name: str) -> None:
    """Every real V3 datum decodes and re-encodes byte-identically."""
    original = _hex(name)
    datum = SaturnSwapSwapDatumV3.from_cbor(original)
    assert datum.to_cbor_hex() == original


@pytest.mark.parametrize("name", _ALL)
def test_v3_flat_output_reference(name: str) -> None:
    """Field 8 is the FLAT OutputReference (tx_id is bare bytes, no wrapper)."""
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex(name))
    assert isinstance(datum.output_reference, SaturnSwapOutputReferenceV3)
    assert isinstance(datum.output_reference.tx_id, bytes)


@pytest.mark.parametrize("name", _UNCOVERED)
def test_v3_uncovered(name: str) -> None:
    """Uncovered orders decode coverage as None; premium accessors are inert."""
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex(name))
    assert isinstance(datum.coverage, PlutusNone)
    assert datum.is_covered() is False
    assert datum.premium_bps() is None
    assert datum.coverage_vault() is None
    assert datum.premium_for_fill(1_000_000) == 0


def test_v3_covered() -> None:
    """A covered order decodes coverage as Some with a premium and vault."""
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex(_COVERED))
    assert isinstance(datum.coverage, SaturnSwapSomeCoverage)
    assert datum.is_covered() is True
    assert datum.premium_bps() == _COVERED_PREMIUM_BPS
    assert isinstance(datum.coverage_vault(), str)  # bech32 vault address


def test_v3_premium_for_fill_formula() -> None:
    """premium_for_fill = max(1, user_sell * premium_bps // 10000)."""
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex(_COVERED))  # premium_bps == 100
    assert datum.premium_for_fill(1_000_000) == _PREMIUM_ON_1M
    assert datum.premium_for_fill(1) == 1  # floored at 1


@pytest.mark.parametrize(("name", "expected"), list(_MIN_PARTIAL.items()))
def test_v3_min_partial_fill(name: str, expected: int) -> None:
    """The min_partial_fill floor decodes to the on-chain value."""
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex(name))
    assert datum.min_partial_fill == expected


def test_v3_order_state_wiring() -> None:
    """The V3 order-state leaf is wired to its datum, script, fee, and address."""
    assert SaturnSwapV3OrderState.order_datum_class() is SaturnSwapSwapDatumV3
    assert SaturnSwapV3OrderState.default_script_class() is PlutusV3Script
    assert SaturnSwapV3OrderState.TAKER_FEE_BPS == _V3_TAKER_FEE_BPS
    assert SaturnSwapV3OrderState.order_selector() == [SATURNSWAP_V3_ORDER_ADDRESS]
    assert SaturnSwapV3OrderState.dex() == "SaturnSwap"
    # V3 is registered at the head so the order book aggregates it first.
    assert _SATURNSWAP_ORDER_STATE_CLASSES[0] is SaturnSwapV3OrderState
    # the V3 order address carries the V3 script payment credential
    assert (
        bytes(Address.decode(SATURNSWAP_V3_ORDER_ADDRESS).payment_part).hex()
        == "6023f59dce0064f1d6d27594dbea25bc4305a9f6a10f3a064037553a"
    )


def test_v3_pricing_parity_with_v2() -> None:
    """V3 inherits pricing unchanged from the shared base (same math + fee)."""
    assert SaturnSwapV3OrderState.TAKER_FEE_BPS == SaturnSwapOrderState.TAKER_FEE_BPS
    assert SaturnSwapV3OrderState.get_amount_out is SaturnSwapOrderState.get_amount_out
    assert SaturnSwapV3OrderState.get_amount_in is SaturnSwapOrderState.get_amount_in


def test_v3_min_partial_fill_guard_rejects_below_floor() -> None:
    """A partial fill below the min_partial_fill floor is rejected."""
    # order_ad182bcd: min_partial_fill = 1_000_000, amount_buy = 3_000_000
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex("order_ad182bcd.hex"))
    with pytest.raises(ValueError, match="min_partial_fill"):
        datum.check_min_partial_fill(500_000)


def test_v3_min_partial_fill_guard_allows_at_or_above_floor_and_full() -> None:
    """Partial at/above the floor and a full fill are allowed."""
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex("order_ad182bcd.hex"))
    datum.check_min_partial_fill(1_500_000)  # partial, at/above floor
    datum.check_min_partial_fill(datum.amount_buy)  # full fill is always allowed


def test_v3_min_partial_fill_guard_no_floor() -> None:
    """With min_partial_fill == 0, any partial is allowed."""
    datum = SaturnSwapSwapDatumV3.from_cbor(_hex("order_18a01339.hex"))  # floor == 0
    datum.check_min_partial_fill(1)


# Real mainnet partial fill: tx ad182bcd... partially filled order b6bcaeb6...#2
# (sell 3000 WIFF / buy 6 ADA) with 3_000_000 lovelace, relisting sell 1500 WIFF /
# buy 3 ADA at ad182bcd#0. The relist datum our builder produces must byte-match
# what mainnet accepted (order_ad182bcd fixture).
_B6BCAE_TX = "b6bcaeb69401127ed650fea550c87464c7fed8aeef05f25950738db6ece754cc"


def test_v3_relist_datum_reconstructs_real_partial() -> None:
    """build_relist_datum reproduces a real on-chain V3 relist byte-for-byte."""
    consumed = SaturnSwapSwapDatumV3.from_cbor(
        _hex("order_b6bcaeb6_out2_cov_none.hex"),
    )
    relist = consumed.build_relist_datum(
        spent_tx_hash=_B6BCAE_TX,
        spent_index=2,
        user_sell_amount=3_000_000,
    )
    assert relist.to_cbor_hex() == _hex("order_ad182bcd.hex")
