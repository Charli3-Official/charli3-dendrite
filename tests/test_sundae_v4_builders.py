"""Byte-exact validation of the SundaeSwap V4 order builders.

``swap_datum`` builds the user's swap-order datum; this asserts the result is
byte-identical to a real on-chain swap order pulled from preview, field by field,
and exercises the owner-cancel redeemer. The reference datum below is public
on-chain data (a live preview swap order at the V4 order validator).
"""

from pycardano import Address
from pycardano import IndefiniteList
from pycardano import Network
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import VerificationKeyHash

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationFixed
from charli3_dendrite.dexs.amm.sundae_v4 import MultisigSignature
from charli3_dendrite.dexs.amm.sundae_v4 import OrderCancel
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SwapConstraint
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CPPState

# A real preview swap order datum (the inline datum of a live order UTxO at the
# V4 order validator). Public on-chain data.
LIVE_SWAP_ORDER_DATUM_HEX = (
    "d8799fd8799f581cb4827ffb1a5f7a8aefb5c6f76cbd1d1db1975e24c478115a083749cbff"
    "d8799fd8799fd8799f581cb4827ffb1a5f7a8aefb5c6f76cbd1d1db1975e24c478115a0837"
    "49cbffd8799fd8799fd8799f581cfa5e11b4390128b6b376ed7bab4b0ffa43afe787140"
    "8aab26bd7d67cffffffffd87a80ff1a002dc6c01927105820000d039b34ea653da4d8321"
    "422e7942e7b621a82d24bb8d2b46b918d83e504fe9f9f581c1a38df57b59e75ad39fdb06f"
    "df8c97ce435297ecfe5b68a3ea523053d87b9fd8799f581cd8906ca5c7ba124a0407a32da"
    "b37b2c82b13b3dcd9111e42940dcea4455553444378ff1a11e1a3001a11e1a3009f9fd879"
    "9f581c45df5f274b8950b512b08d10656864958659c4ecf3ffad092ef630244455534472f"
    "f1a118f9b00ffffffff9f581cef81595b5b8cf9bc5f0adfb0b8f3a2d60edef9d33755ca87"
    "fa86c07780ff9f581cb0df1c266988ab3bb5497bf9f6d8749a5f7726e88d43fca52efaa7f"
    "4d87980ffffd87980ff"
)

# The order owner / destination (a base address: payment key + stake key).
_OWNER_PAYMENT_VKH = "b4827ffb1a5f7a8aefb5c6f76cbd1d1db1975e24c478115a083749cb"
_OWNER_STAKE_VKH = "fa5e11b4390128b6b376ed7bab4b0ffa43afe7871408aab26bd7d67c"

# The offered asset (USDCx) and the asked asset (USDr) with the live amounts.
_OFFERED_UNIT = "d8906ca5c7ba124a0407a32dab37b2c82b13b3dcd9111e42940dcea45553444378"
_ASKED_UNIT = "45df5f274b8950b512b08d10656864958659c4ecf3ffad092ef6302455534472"
_OFFERED_AMOUNT = 300_000_000
_MIN_RECEIVED = 294_624_000


def _swap_leg() -> _SundaeV4CPPState:
    """A projected V4 constant-product leg (only the order builders are exercised)."""
    return _SundaeV4CPPState.model_validate(
        {
            "assets": Assets(**{"lovelace": 1_000_000, _OFFERED_UNIT: 500}),
            "block_time": 0,
            "block_index": 0,
            "plutus_v2": True,
            "datum_cbor": "00",
            "datum_hash": "00",
            "tx_index": 0,
            "tx_hash": "00",
            "fee": 30,
        },
    )


def _source_address() -> Address:
    return Address(
        payment_part=VerificationKeyHash(bytes.fromhex(_OWNER_PAYMENT_VKH)),
        staking_part=VerificationKeyHash(bytes.fromhex(_OWNER_STAKE_VKH)),
        network=Network.TESTNET,
    )


def _build_swap_datum() -> SundaeV4OrderDatum:
    return _swap_leg().swap_datum(
        address_source=_source_address(),
        in_assets=Assets(**{_OFFERED_UNIT: _OFFERED_AMOUNT}),
        out_assets=Assets(**{_ASKED_UNIT: _MIN_RECEIVED}),
        budget=3_000_000,
        share_batcher=10_000,
    )


def test_swap_datum_is_byte_exact_with_live_order() -> None:
    """The built swap-order datum re-encodes byte-for-byte to the live order."""
    built = _build_swap_datum().to_cbor_hex()
    assert built == LIVE_SWAP_ORDER_DATUM_HEX


def test_swap_datum_round_trips_through_parser() -> None:
    """The built datum parses back to the same seven-field order datum."""
    built = _build_swap_datum()
    reparsed = SundaeV4OrderDatum.from_cbor(built.to_cbor_hex())
    assert reparsed.to_cbor_hex() == LIVE_SWAP_ORDER_DATUM_HEX


def test_swap_datum_field_shape() -> None:
    """The seven controlled fields carry the expected values."""
    datum = _build_swap_datum()

    assert isinstance(datum.owner, MultisigSignature)
    assert datum.owner.key_hash == bytes.fromhex(_OWNER_PAYMENT_VKH)

    assert isinstance(datum.destination, DestinationFixed)
    # The destination resolves back to the owner's payment + stake credentials
    # (``to_address`` carries no network discriminator, so compare the parts).
    resolved = datum.destination.address.to_address()
    source = _source_address()
    assert bytes(resolved.payment_part) == bytes(source.payment_part)
    assert bytes(resolved.staking_part) == bytes(source.staking_part)
    # ``Option<Data>`` None == constructor 1.
    assert datum.destination.datum.data.tag == 122

    assert datum.budget == 3_000_000
    assert datum.share_batcher == 10_000

    # The swap-role order-config token is present (the seventh-field binding).
    assert datum.config_token == bytes.fromhex(
        "000d039b34ea653da4d8321422e7942e7b621a82d24bb8d2b46b918d83e504fe",
    )

    assert isinstance(datum.extension, RawPlutusData)
    assert datum.extension.data.tag == 121


def test_constraints_are_the_swap_role_keyed_list() -> None:
    """``constraints`` is the keyed list of the swap role's three modules, in order."""
    datum = _build_swap_datum()
    constraints = list(datum.constraints)
    assert isinstance(datum.constraints, IndefiniteList)
    assert len(constraints) == 3

    swap_entry, route_entry, fairness_entry = (list(c) for c in constraints)

    assert swap_entry[0] == bytes.fromhex(
        "1a38df57b59e75ad39fdb06fdf8c97ce435297ecfe5b68a3ea523053",
    )
    assert route_entry[0] == bytes.fromhex(
        "ef81595b5b8cf9bc5f0adfb0b8f3a2d60edef9d33755ca87fa86c077",
    )
    assert fairness_entry[0] == bytes.fromhex(
        "b0df1c266988ab3bb5497bf9f6d8749a5f7726e88d43fca52efaa7f4",
    )

    # The route payload is the empty list; the fairness payload is the empty
    # constructor-0 record — both are no-ops for a plain swap.
    assert route_entry[1] == []
    assert isinstance(fairness_entry[1], RawPlutusData)
    assert fairness_entry[1].data.tag == 121


def test_swap_constraint_payload_decodes() -> None:
    """The swap constraint payload (constructor 2) carries the offered/ask shape."""
    datum = _build_swap_datum()
    swap_entry = list(list(datum.constraints)[0])
    swap = swap_entry[1]
    assert isinstance(swap, SwapConstraint)
    # The constraint-tag namespace assigns 2 to a swap (CBOR tag 123).
    assert swap.CONSTR_ID == 2
    assert swap.to_cbor_hex().startswith("d87b")

    assert swap.offered == AssetClass(
        policy=bytes.fromhex(_OFFERED_UNIT[:56]),
        asset_name=bytes.fromhex(_OFFERED_UNIT[56:]),
    )
    assert swap.original_offered == _OFFERED_AMOUNT
    assert swap.remaining_offered == _OFFERED_AMOUNT

    min_received = list(swap.min_received)
    assert len(min_received) == 1
    asset, amount = list(min_received[0])
    assert asset == AssetClass(
        policy=bytes.fromhex(_ASKED_UNIT[:56]),
        asset_name=bytes.fromhex(_ASKED_UNIT[56:]),
    )
    assert amount == _MIN_RECEIVED


def test_cancel_redeemer_is_owner_cancel() -> None:
    """The cancel redeemer is the order ``Cancel`` variant (constructor 0)."""
    redeemer = _SundaeV4CPPState.cancel_redeemer()
    assert isinstance(redeemer, Redeemer)
    assert isinstance(redeemer.data, OrderCancel)
    assert redeemer.data.to_cbor_hex() == "d87980"
