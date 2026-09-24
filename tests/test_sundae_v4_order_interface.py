"""SundaeV4OrderDatum implements dendrite's OrderDatum interface."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pycardano import IndefiniteList
from pycardano import RawPlutusData
from pycardano.serialization import CBORTag

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dexs.amm.sundae_v4 import AssetClass
from charli3_dendrite.dexs.amm.sundae_v4 import BasicSwap
from charli3_dendrite.dexs.amm.sundae_v4 import BasicWithdraw
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationFixed
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationSelf
from charli3_dendrite.dexs.amm.sundae_v4 import MultisigSignature
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from tests.test_sundae_v4_builders import _ADMIN_PAYMENT
from tests.test_sundae_v4_builders import _ADMIN_STAKE
from tests.test_sundae_v4_builders import _MNGO_STRW_LP
from tests.test_sundae_v4_builders import _USER_PAYMENT
from tests.test_sundae_v4_builders import _USER_STAKE
from tests.test_sundae_v4_builders import _address
from tests.test_sundae_v4_builders import _FixedFeeSettingsBackend
from tests.test_sundae_v4_builders import _pool

_AUDITFINAL = json.loads(
    (Path(__file__).parent / "sundae_v4_auditfinal_fixtures.json").read_text(),
)
_MAINNET = json.loads(
    (Path(__file__).parent / "sundae_v4_mainnet_fixtures.json").read_text(),
)
_ORDER_DATUMS = _AUDITFINAL["order_datums"]
_MAINNET_ORDERS = _MAINNET["orders"]

_KNOWN_WALLET_CREDENTIALS = {
    (_ADMIN_PAYMENT, _ADMIN_STAKE),
    (_USER_PAYMENT, _USER_STAKE),
}


@pytest.fixture(autouse=True)
def _restore_default_network() -> Iterator[None]:
    """Guarantee a deterministic base fee and the mainnet default for each test.

    ``_pool()`` (imported from the builders test module) may point the class
    family at a testnet deployment, and a built strategy datum's defaulted fee
    fields call ``SundaeV4Vault.base_fee()`` against whatever backend is
    active; this clears the base-fee cache, installs a backend fixed at the
    builders module's ``_FEE_AT_CAPTURE`` so that lookup can never reach a
    real (or unset) backend, and restores mainnet regardless of outcome so no
    test's network choice can leak into the next.
    """
    SundaeV4Vault.clear_base_fee_cache()
    set_backend(_FixedFeeSettingsBackend())
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_base_fee_cache()


def _decode(cbor_hex: str) -> SundaeV4OrderDatum:
    """Decode a recorded order datum's hex CBOR."""
    return SundaeV4OrderDatum.from_cbor(bytes.fromhex(cbor_hex))


def _assets_from_pairs(pairs) -> Assets:  # noqa: ANN001
    """Dendrite ``Assets`` from a decoded ``[AssetClass, amount]`` pair list."""
    out: dict[str, int] = {}
    for entry in pairs:
        fields = list(entry)
        asset_class = AssetClass.from_primitive(fields[0])
        unit = (asset_class.policy.hex() + asset_class.asset_name.hex()) or "lovelace"
        out[unit] = int(fields[1])
    return Assets(**out)


_ALL_RECORDED = [
    pytest.param(o["datum"], id=f"auditfinal:{o['label']}") for o in _ORDER_DATUMS
] + [pytest.param(o["datum"], id=f"mainnet:{i}") for i, o in enumerate(_MAINNET_ORDERS)]


@pytest.mark.parametrize("cbor_hex", _ALL_RECORDED)
def test_every_recorded_order_is_an_order_datum(cbor_hex: str) -> None:
    assert isinstance(_decode(cbor_hex), OrderDatum)


@pytest.mark.parametrize(
    "entry",
    [pytest.param(o, id=o["label"]) for o in _ORDER_DATUMS if "swap" in o["label"]],
)
def test_recorded_basic_swaps_classify_as_swap_and_match_min_received(
    entry: dict,
) -> None:
    datum = _decode(entry["datum"])
    assert datum.order_type() == OrderType.swap
    basic = datum.basic_constraint()
    assert datum.requested_amount() == _assets_from_pairs(list(basic.min_received))


def test_recorded_preview_deposit_classifies_as_deposit_with_the_lp_unit() -> None:
    entry = next(
        o
        for o in _ORDER_DATUMS
        if o["label"] == "basic deposit MNGO+STRW -> LP (ctor 0)"
    )
    datum = _decode(entry["datum"])
    assert datum.order_type() == OrderType.deposit
    assert datum.requested_amount().unit() == _MNGO_STRW_LP


def test_mainnet_withdraw_order_classifies_as_withdraw() -> None:
    decoded = [_decode(o["datum"]) for o in _MAINNET_ORDERS]
    withdraws = [d for d in decoded if isinstance(d.basic_constraint(), BasicWithdraw)]
    assert len(withdraws) == 1
    assert withdraws[0].order_type() == OrderType.withdraw


def test_built_strategy_datum_classifies_as_swap_with_no_floor_and_no_source() -> None:
    pool = _pool()
    built = pool.strategy_datum(
        address_source=_address(_USER_PAYMENT, _USER_STAKE),
        auth=MultisigSignature(key_hash=bytes.fromhex(_ADMIN_PAYMENT)),
        final_destinations=[_address(_USER_PAYMENT, _USER_STAKE)],
    )
    assert isinstance(built.destination, DestinationSelf)
    assert built.order_type() == OrderType.swap
    assert built.requested_amount() == Assets({})
    assert built.address_source() is None


def test_built_strategy_datum_with_a_target_reports_it_as_the_source() -> None:
    pool = _pool()
    target = _address(_ADMIN_PAYMENT, _ADMIN_STAKE)
    built = pool.strategy_datum(
        address_source=_address(_USER_PAYMENT, _USER_STAKE),
        auth=MultisigSignature(key_hash=bytes.fromhex(_ADMIN_PAYMENT)),
        final_destinations=[_address(_USER_PAYMENT, _USER_STAKE)],
        address_target=target,
    )
    assert isinstance(built.destination, DestinationFixed)
    resolved = built.address_source()
    assert resolved is not None
    # PlutusFullAddress.to_address() does not carry a network tag (it is not
    # part of the on-chain datum), so compare credentials rather than the
    # bech32 encoding, which would otherwise differ by network.
    assert resolved.payment_part == target.payment_part
    assert resolved.staking_part == target.staking_part


def test_order_type_and_requested_amount_raise_when_no_known_constraint_key_matches() -> (
    None
):
    unknown_payload = RawPlutusData(CBORTag(121, []))
    datum = SundaeV4OrderDatum(
        owner=MultisigSignature(key_hash=b"\x00" * 28),
        destination=DestinationSelf(),
        service_budget=1_000_000,
        max_per_execution=1_000_000,
        config_token=b"\x00" * 32,
        constraints=IndefiniteList([IndefiniteList([b"\xff" * 28, unknown_payload])]),
        extension=unknown_payload,
    )
    with pytest.raises(ValueError, match="no recognised"):
        datum.order_type()
    with pytest.raises(ValueError, match="no recognised"):
        datum.requested_amount()


@pytest.mark.parametrize(
    "entry",
    [pytest.param(o, id=o["label"]) for o in _ORDER_DATUMS],
)
def test_auditfinal_fixed_destination_matches_a_known_wallet(entry: dict) -> None:
    """``address_source()`` resolves to one of the two known auditfinal wallets.

    Checked against the admin/user payment+stake key hashes the builder
    tests independently know, not re-derived from the same destination field
    the method under test reads, so this is not circular.
    """
    datum = _decode(entry["datum"])
    if not isinstance(datum.destination, DestinationFixed):
        pytest.skip("not a fixed-destination order")
    resolved = datum.address_source()
    assert resolved is not None
    payment_hex = bytes(resolved.payment_part).hex()
    stake_hex = bytes(resolved.staking_part).hex() if resolved.staking_part else None
    assert (payment_hex, stake_hex) in _KNOWN_WALLET_CREDENTIALS


@pytest.mark.parametrize(
    "entry",
    [pytest.param(o, id=f"mainnet:{i}") for i, o in enumerate(_MAINNET_ORDERS)],
)
def test_mainnet_fixed_destination_matches_the_orders_own_owner_credential(
    entry: dict,
) -> None:
    """``address_source()`` carries the same key hash as the order's own owner.

    Cross-checked against the datum's ``owner`` field — a different field
    from ``destination`` — rather than re-deriving the expected value from
    the same expression ``address_source()`` itself evaluates.
    """
    datum = _decode(entry["datum"])
    assert isinstance(datum.destination, DestinationFixed)
    assert isinstance(datum.owner, MultisigSignature)
    resolved = datum.address_source()
    assert resolved is not None
    payment_hex = bytes(resolved.payment_part).hex()
    stake_hex = bytes(resolved.staking_part).hex() if resolved.staking_part else None
    assert datum.owner.key_hash.hex() in (payment_hex, stake_hex)


@pytest.mark.parametrize(
    "entry",
    [pytest.param(o, id=f"mainnet:{i}") for i, o in enumerate(_MAINNET_ORDERS)],
)
def test_every_mainnet_basic_swap_requested_amount_matches_min_received(
    entry: dict,
) -> None:
    datum = _decode(entry["datum"])
    basic = datum.basic_constraint()
    if not isinstance(basic, BasicSwap):
        pytest.skip("not a basic swap")
    assert datum.requested_amount() == _assets_from_pairs(list(basic.min_received))
