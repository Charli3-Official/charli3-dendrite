"""Swap forwarding: ``create_datum`` routes the filled output to a TARGET
address with an optional inline receiver datum (so it can fund the next order in
a cross-protocol chain) instead of returning to the source wallet, across
Minswap V2 / SundaeV3 / WingRiders V2.

Pure-unit: calls ``create_datum`` directly (no pool instance / backend).
"""

from __future__ import annotations

import pytest
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.minswap import MinswapV2OrderDatum
from charli3_dendrite.dexs.amm.sundae import SundaeV3OrderDatum
from charli3_dendrite.dexs.amm.sundae import SundaeV3PlutusNone
from charli3_dendrite.dexs.amm.sundae import SundaeV3ReceiverInlineDatum
from charli3_dendrite.dexs.amm.wingriders import InlineDatum
from charli3_dendrite.dexs.amm.wingriders import NoDatum
from charli3_dendrite.dexs.amm.wingriders import WingRidersV2OrderDatum
from pycardano import Address
from pycardano import Network
from pycardano import PlutusData
from pycardano import RawPlutusData
from pycardano import VerificationKeyHash

_SRC = Address(
    VerificationKeyHash(b"\x11" * 28),
    VerificationKeyHash(b"\x22" * 28),
    network=Network.MAINNET,
)
_TGT = Address(
    VerificationKeyHash(b"\x33" * 28),
    VerificationKeyHash(b"\x44" * 28),
    network=Network.MAINNET,
)
_IN = Assets(root={"lovelace": 10_000_000})
_OUT = Assets(root={"a" * 56 + "deadbeef": 5_000_000})
_BATCHER = Assets(root={"lovelace": 2_000_000})
_DEPOSIT = Assets(root={"lovelace": 2_000_000})


@pytest.fixture()
def datum_target() -> PlutusData:
    """An arbitrary inline datum to forward into the next order."""
    return PlutusData()


# ── Minswap V2 ──
def test_minswap_v2_forwards_to_target(datum_target: PlutusData) -> None:
    d = MinswapV2OrderDatum.create_datum(
        address_source=_SRC,
        in_assets=_IN,
        out_assets=_OUT,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
        address_target=_TGT,
        datum_target=datum_target,
    )
    assert d.receiver_address == PlutusFullAddress.from_address(_TGT)
    # datum_target is the INNER next-hop datum; Minswap wraps it as a
    # SundaeV3ReceiverInlineDatum on the receiver.
    assert isinstance(d.receiver_datum_hash, SundaeV3ReceiverInlineDatum)
    assert d.receiver_datum_hash.datum == datum_target
    # Only the receiver carries the forward datum; the refund path (back to the
    # source wallet) carries none.
    assert isinstance(d.refund_datum_hash, SundaeV3PlutusNone)


def test_minswap_v2_no_target_returns_to_source() -> None:
    d = MinswapV2OrderDatum.create_datum(
        address_source=_SRC,
        in_assets=_IN,
        out_assets=_OUT,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
    )
    assert d.receiver_address == PlutusFullAddress.from_address(_SRC)


# ── SundaeV3 ──
def test_sundae_v3_forwards_to_target(datum_target: PlutusData) -> None:
    d = SundaeV3OrderDatum.create_datum(
        ident=b"\x00" * 28,
        address_source=_SRC,
        in_assets=_IN,
        out_assets=_OUT,
        fee=1_000_000,
        address_target=_TGT,
        datum_target=datum_target,
    )
    assert d.destination.address == PlutusFullAddress.from_address(_TGT)
    assert isinstance(d.destination.datum, SundaeV3ReceiverInlineDatum)
    assert d.destination.datum.datum == datum_target


def test_sundae_v3_no_target_returns_to_source() -> None:
    d = SundaeV3OrderDatum.create_datum(
        ident=b"\x00" * 28,
        address_source=_SRC,
        in_assets=_IN,
        out_assets=_OUT,
        fee=1_000_000,
    )
    assert d.destination.address == PlutusFullAddress.from_address(_SRC)
    assert isinstance(d.destination.datum, SundaeV3PlutusNone)


# ── WingRiders V2 ──
def test_wingriders_v2_forwards_to_target(datum_target: PlutusData) -> None:
    d = WingRidersV2OrderDatum.create_datum(
        address_source=_SRC,
        in_assets=_IN,
        out_assets=_OUT,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
        address_target=_TGT,
        datum_target=datum_target,
    )
    # beneficiary (the fill recipient) = target; owner stays the source.
    assert d.beneficiary == PlutusFullAddress.from_address(_TGT)
    assert d.owner_address == PlutusFullAddress.from_address(_SRC)
    assert isinstance(d.compensation_datum_type, InlineDatum)
    # the inline compensation datum is the RAW datum object, not its CBOR bytes.
    assert d.compensation_datum == datum_target


def test_wingriders_v2_no_target_returns_to_owner() -> None:
    d = WingRidersV2OrderDatum.create_datum(
        address_source=_SRC,
        in_assets=_IN,
        out_assets=_OUT,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
    )
    assert d.beneficiary == PlutusFullAddress.from_address(_SRC)
    assert isinstance(d.compensation_datum_type, NoDatum)


def test_wingriders_v2_forwards_rawplutusdata_target() -> None:
    """A RawPlutusData next-hop (e.g. a multi-leg inner datum parsed from CBOR)
    must serialize. WR's strict ``compensation_datum`` Union rejects the
    RawPlutusData *class* at serialization (``.hash()``, which ``swap_utxo``
    calls) while Sundae/Minswap accept it — so create_datum unwraps it to its
    raw ``.data``. Typed and raw next-hops must yield byte-identical WR datums.
    """
    inner = MinswapV2OrderDatum.create_datum(
        address_source=_TGT,
        in_assets=_IN,
        out_assets=_OUT,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
    )
    raw = RawPlutusData.from_cbor(inner.to_cbor_hex())
    kw = {
        "address_source": _SRC,
        "in_assets": _IN,
        "out_assets": _OUT,
        "batcher_fee": _BATCHER,
        "deposit": _DEPOSIT,
        "address_target": _TGT,
    }
    d_typed = WingRidersV2OrderDatum.create_datum(datum_target=inner, **kw)
    d_raw = WingRidersV2OrderDatum.create_datum(datum_target=raw, **kw)
    # both serialize/hash cleanly (this is what swap_utxo does)...
    assert d_raw.hash() is not None
    assert isinstance(d_raw.compensation_datum_type, InlineDatum)
    # ...and the raw wrapper produces the SAME datum as the typed next-hop.
    assert d_raw.hash() == d_typed.hash()
