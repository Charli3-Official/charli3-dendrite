"""Reconstruct real mainnet forwarded orders and assert the forwarding fields
rebuild correctly.

Fixtures are real mainnet forwarded orders pinned in
``tests/data/forwarding_fixtures.json`` so this runs offline. For each we parse
the order datum, feed its forwarding info (source / target / inner next-hop
datum) back through ``create_datum``, and assert the rebuilt forwarding fields
match the original datum byte-for-byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.minswap import MinswapV2OrderDatum
from charli3_dendrite.dexs.amm.sundae import SundaeV3OrderDatum
from charli3_dendrite.dexs.amm.sundae import SundaeV3PlutusNone
from charli3_dendrite.dexs.amm.sundae import SundaeV3ReceiverInlineDatum
from charli3_dendrite.dexs.amm.wingriders import HashDatum
from charli3_dendrite.dexs.amm.wingriders import InlineDatum
from charli3_dendrite.dexs.amm.wingriders import NoDatum
from charli3_dendrite.dexs.amm.wingriders import WingRidersV2OrderDatum
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

_FIX = json.loads(
    (Path(__file__).parent / "data" / "forwarding_fixtures.json").read_text(),
)
_IN = Assets(root={"lovelace": 1_000_000})
_DUMMY_OUT = Assets(root={"a" * 56 + "deadbeef": 1})
_BATCHER = Assets(root={"lovelace": 2_000_000})
_DEPOSIT = Assets(root={"lovelace": 2_000_000})
# A stake-bearing dummy source. Sundae's owner is only a stake-key hash (the
# datum doesn't carry the full source address), and forward TARGETS are often
# enterprise script addresses — so we reconstruct with a fixed source and assert
# only the destination (the forwarding fields) matches the on-chain order.
_SUNDAE_SRC = Address(
    VerificationKeyHash(b"\x11" * 28),
    VerificationKeyHash(b"\x22" * 28),
    network=Network.MAINNET,
)


def _check_minswap_v2(cbor: str) -> None:
    d = MinswapV2OrderDatum.from_cbor(cbor)
    rb = MinswapV2OrderDatum.create_datum(
        address_source=d.refund_address.to_address(),
        in_assets=_IN,
        out_assets=_DUMMY_OUT,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
        address_target=d.receiver_address.to_address(),
        datum_target=d.receiver_datum_hash.datum,  # inner next-hop datum
    )
    assert rb.receiver_address == d.receiver_address
    assert rb.receiver_datum_hash == d.receiver_datum_hash
    assert rb.refund_datum_hash == d.refund_datum_hash  # PlutusNone
    assert rb.owner == d.owner


def _check_wingriders_v2(cbor: str) -> None:
    d = WingRidersV2OrderDatum.from_cbor(cbor)
    if not isinstance(d.compensation_datum_type, InlineDatum):
        # WR's aggregator forwarding adapts to the DESTINATION's datum
        # convention: InlineDatum into modern inline-datum DEXes (Minswap V2 /
        # Sundae V3 / WR V2), HashDatum into legacy hash-datum DEXes (Minswap V1
        # / Sundae V1 / Muesli), and occasionally NoDatum. Our forwarding only
        # targets modern inline-datum DEXes, so create_datum builds InlineDatum
        # only — the hash/none modes are out of scope (documented, not rebuilt).
        assert isinstance(d.compensation_datum_type, (HashDatum, NoDatum))
        pytest.skip(
            f"WR non-inline forward mode: {type(d.compensation_datum_type).__name__}",
        )
    rb = WingRidersV2OrderDatum.create_datum(
        address_source=d.owner_address.to_address(),
        in_assets=_IN,
        out_assets=_DUMMY_OUT,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
        address_target=d.beneficiary.to_address(),  # may be enterprise (no stake)
        datum_target=d.compensation_datum,  # raw inner datum
    )
    assert rb.beneficiary == d.beneficiary
    assert rb.owner_address == d.owner_address
    assert rb.compensation_datum == d.compensation_datum
    assert isinstance(rb.compensation_datum_type, InlineDatum)
    assert isinstance(d.compensation_datum_type, InlineDatum)


def _check_sundae_v3(cbor: str) -> None:
    d = SundaeV3OrderDatum.from_cbor(cbor)
    if isinstance(d.destination.datum, SundaeV3PlutusNone):
        pytest.skip("Sundae V3 order is terminal (no forward)")
    if not isinstance(d.destination.datum, SundaeV3ReceiverInlineDatum):
        # Like WingRiders, Sundae has a datum-HASH forward mode
        # (SundaeV3ReceiverDatumHash) alongside the inline mode. create_datum
        # builds the inline next-hop datum only; the hash mode is out of scope
        # (documented, not rebuilt).
        pytest.skip(
            f"Sundae non-inline forward mode: {type(d.destination.datum).__name__}",
        )
    # Sundae forwards via destination = (target address, inline next-hop datum).
    # The next hop is itself an order datum (a second Sundae order for internal
    # chaining, or another DEX's order for a cross-protocol hop), so the forward
    # is recursive — multi-leg composes with no extra code. We rebuild the
    # destination and assert it matches the original datum byte-for-byte.
    assert isinstance(d.destination.datum, SundaeV3ReceiverInlineDatum)
    rb = SundaeV3OrderDatum.create_datum(
        ident=b"\x00" * 28,
        address_source=_SUNDAE_SRC,
        in_assets=_IN,
        out_assets=_DUMMY_OUT,
        fee=d.max_protocol_fee,
        address_target=d.destination.address.to_address(),
        datum_target=d.destination.datum.datum,  # inner next-hop order datum
    )
    assert rb.destination.address == d.destination.address
    assert rb.destination.datum == d.destination.datum


_HANDLERS: dict[str, Any] = {
    "MinswapV2": _check_minswap_v2,
    "WingRidersV2": _check_wingriders_v2,
    "SundaeV3": _check_sundae_v3,
}


@pytest.mark.parametrize(
    "fx",
    _FIX,
    ids=[f"{f['source']}_to_{f['dest']}" for f in _FIX],
)
def test_reconstruct_forwarded_order(fx: dict) -> None:
    handler = _HANDLERS.get(fx["source"])
    if handler is None:
        pytest.skip(f"no reconstruction handler for source DEX {fx['source']}")
    handler(fx["datum_cbor"])
