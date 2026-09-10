"""Byte-exact validation of the SundaeSwap V4 order builders and deployment manifest.

The reference datums are real inline datums of orders placed on the audit-final
preview and preprod deployments (public on-chain data, captured in
``sundae_v4_auditfinal_fixtures.json``). Each builder must re-encode
byte-for-byte to a live order of its kind, resolving every hash and config token
from the per-network deployment manifest rather than from constants.
"""

import json
from pathlib import Path

import pytest
from pycardano import Address
from pycardano import IndefiniteList
from pycardano import Network
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import VerificationKeyHash
from pycardano.serialization import CBORTag

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import BasicConstraintKind
from charli3_dendrite.dexs.amm.sundae_v4 import BasicDeposit
from charli3_dendrite.dexs.amm.sundae_v4 import BasicSwap
from charli3_dendrite.dexs.amm.sundae_v4 import BoolTrue
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationFixed
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationSelf
from charli3_dendrite.dexs.amm.sundae_v4 import IntervalBound
from charli3_dendrite.dexs.amm.sundae_v4 import IntervalBoundFinite
from charli3_dendrite.dexs.amm.sundae_v4 import IntervalBoundPositiveInfinity
from charli3_dendrite.dexs.amm.sundae_v4 import MultisigSignature
from charli3_dendrite.dexs.amm.sundae_v4 import OptionSomeInt
from charli3_dendrite.dexs.amm.sundae_v4 import OrderCancel
from charli3_dendrite.dexs.amm.sundae_v4 import OutputReference
from charli3_dendrite.dexs.amm.sundae_v4 import SignedStrategyExecution
from charli3_dendrite.dexs.amm.sundae_v4 import StrategyConstraint
from charli3_dendrite.dexs.amm.sundae_v4 import StrategyExecution
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import ValidityRange
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CSState
from charli3_dendrite.dexs.amm.sundae_v4 import parse_basic_constraint

FIXTURES = json.loads(
    (Path(__file__).parent / "sundae_v4_auditfinal_fixtures.json").read_text(),
)
ORDERS = {o["label"]: o for o in FIXTURES["order_datums"]}

# The deployment admin wallet (payment key + stake key) that placed the
# preview / preprod reference swaps, and the second preview wallet that placed
# the deposit and the lovelace-offered swap (owner keyed on its stake key).
_ADMIN_PAYMENT = "e2afcadc7b111be7b89f283e9facffbbc5292f40fd13d7613e639c35"
_ADMIN_STAKE = "19f8f253c67c91082c43d306cd4edf825494ff8dc69b6678b83aa669"
_USER_PAYMENT = "dc3b848b6d09999837cb1f5e6e5b0b51b27127b8046d3a910168d39e"
_USER_STAKE = "1ce3f1c994be8ff33d2a832129b3928f28339fc01119262b1a3e5152"

_TOKEN_POLICY = "09169bb6f5ff5b246d65d65935b2222cc53b5e677d7ed22771878972"
_MINT = _TOKEN_POLICY + "4d494e54"
_MNGO = _TOKEN_POLICY + "4d4e474f"
_STRW = _TOKEN_POLICY + "53545257"
_MNGO_STRW_LP = (
    "b8a18e251b3e5078c04203f1a9af52408064c5d3a4256cfcfe3f67dd"
    "0014df1047953ac826cf9a370772bd596009bd6ed7bd7652ef20554b38a1292c"
)


def _address(payment: str, stake: str) -> Address:
    return Address(
        payment_part=VerificationKeyHash(bytes.fromhex(payment)),
        staking_part=VerificationKeyHash(bytes.fromhex(stake)),
        network=Network.TESTNET,
    )


def _leg() -> _SundaeV4CSState:
    """A projected V4 constant-sum leg (only the order builders are exercised)."""
    return _SundaeV4CSState.model_validate(
        {
            "assets": Assets(**{_MINT: 1_000, _STRW: 2_000}),
            "block_time": 0,
            "block_index": 0,
            "plutus_v2": True,
            "datum_cbor": "00",
            "datum_hash": "00",
            "tx_index": 0,
            "tx_hash": "00",
        },
    )


@pytest.fixture
def preprod():
    """Point the class family at the preprod deployment for one test."""
    _SundaeV4CSState.select_network("preprod")
    try:
        yield
    finally:
        _SundaeV4CSState.select_network("preview")


# ---------------------------------------------------------------------------
# Deployment manifest
# ---------------------------------------------------------------------------


def test_manifest_resolves_the_preview_deployment() -> None:
    deployment = SundaeV4Deployment.for_network("preview")
    assert (
        deployment.pool_hash.hex()
        == "f577d24cfa393efdb85482554acbdd5082f99acba9c17eab916cf87a"
    )
    assert (
        deployment.order_hash.hex()
        == "8a7ecdafb3605ddf761751b391c20482b7ef42ca01575d135d484c2b"
    )
    assert (
        deployment.basic_order_hash.hex()
        == "4859acf5f46a50f383d16323a3c7eacf502ba732aa8874a7d3b7783e"
    )
    assert (
        deployment.strategy_order_hash.hex()
        == "5b67b76ff03dd497083df1450caabb3a4a99feea53ad631e700173ee"
    )
    assert (
        deployment.fee_constraint_hash.hex()
        == "d7d1c09deb8bc8e15baae7895e57e86ab6d0cb10e2faaa6d96cd691b"
    )
    assert (
        deployment.pool_nft_policy.hex()
        == "b8a18e251b3e5078c04203f1a9af52408064c5d3a4256cfcfe3f67dd"
    )
    assert (
        deployment.basic_config_token.hex()
        == "0056667008c14652ef9039690587c17fa70546e3ccd62ec63ec750cfd72969ab"
    )
    assert (
        deployment.strategy_config_token.hex()
        == "00e3317090f21164a1042f9a5d0aa07d585d7fdf6e4439788955c2384d844078"
    )
    assert deployment.base_fee == 1_000_000
    assert deployment.reference("order.spend") == (
        "cfd46884fdf1b6b2a91feb6ad7252f950e5dad80ad83b2939954795c093925da",
        0,
    )


def test_manifest_has_no_mainnet_deployment_yet() -> None:
    with pytest.raises(LookupError, match="mainnet"):
        SundaeV4Deployment.for_network("mainnet")


def test_class_family_defaults_to_preview_and_can_switch(preprod) -> None:
    address = Address.decode(_SundaeV4CSState.pool_selector().addresses[0])
    assert bytes(address.payment_part).hex().startswith("ae364bd4")
    order = Address.decode(_SundaeV4CSState.order_selector()[0])
    assert bytes(order.payment_part).hex().startswith("2d066c46")


def test_class_family_is_back_on_preview_after_the_switch() -> None:
    address = Address.decode(_SundaeV4CSState.pool_selector().addresses[0])
    assert bytes(address.payment_part).hex().startswith("f577d24c")


# ---------------------------------------------------------------------------
# swap_datum: the routing-free basic swap (constructor 2)
# ---------------------------------------------------------------------------


def test_swap_datum_is_byte_exact_with_the_preview_swap() -> None:
    reference = ORDERS["basic swap STRW->MINT (ctor 2, fee-bearing)"]["datum"]
    built = _leg().swap_datum(
        address_source=_address(_ADMIN_PAYMENT, _ADMIN_STAKE),
        in_assets=Assets(**{_STRW: 2_000_000_000}),
        out_assets=Assets(**{_MINT: 990_000_000}),
        owner=MultisigSignature(key_hash=bytes.fromhex(_ADMIN_PAYMENT)),
        service_budget=3_000_000,
        max_per_execution=3_000_000,
    )
    assert built.to_cbor_hex() == reference


def test_swap_datum_is_byte_exact_with_the_preprod_swap(preprod) -> None:
    reference = ORDERS["basic swap MINT->STRW (preprod)"]["datum"]
    built = _leg().swap_datum(
        address_source=_address(_ADMIN_PAYMENT, _ADMIN_STAKE),
        in_assets=Assets(**{_MINT: 500_000_000}),
        out_assets=Assets(**{_STRW: 990_000_000}),
        owner=MultisigSignature(key_hash=bytes.fromhex(_ADMIN_PAYMENT)),
        service_budget=3_000_000,
        max_per_execution=3_000_000,
    )
    assert built.to_cbor_hex() == reference


def test_swap_datum_offering_lovelace_keys_the_owner_on_the_stake_key() -> None:
    reference = ORDERS["basic swap ADA->STRW (ctor 2, ADA offered, mpe 1 ADA)"]["datum"]
    built = _leg().swap_datum(
        address_source=_address(_USER_PAYMENT, _USER_STAKE),
        in_assets=Assets(lovelace=100_300_903),
        out_assets=Assets(**{_STRW: 80_000_000}),
        service_budget=3_000_000,
        max_per_execution=1_000_000,
    )
    assert built.to_cbor_hex() == reference
    assert built.owner == MultisigSignature(key_hash=bytes.fromhex(_USER_STAKE))


def test_swap_datum_defaults_pay_exactly_the_base_fee() -> None:
    built = _leg().swap_datum(
        address_source=_address(_ADMIN_PAYMENT, _ADMIN_STAKE),
        in_assets=Assets(**{_STRW: 1}),
        out_assets=Assets(**{_MINT: 1}),
    )
    assert built.service_budget == built.max_per_execution == 1_000_000


def test_swap_datum_field_shape() -> None:
    built = _leg().swap_datum(
        address_source=_address(_ADMIN_PAYMENT, _ADMIN_STAKE),
        in_assets=Assets(**{_STRW: 2_000_000_000}),
        out_assets=Assets(**{_MINT: 990_000_000}),
    )
    assert isinstance(built.destination, DestinationFixed)
    resolved = built.destination.address.to_address()
    assert bytes(resolved.payment_part).hex() == _ADMIN_PAYMENT
    assert bytes(resolved.staking_part).hex() == _ADMIN_STAKE
    assert built.destination.datum.data.tag == 122  # Option<Data> None
    assert (
        built.config_token
        == SundaeV4Deployment.for_network("preview").basic_config_token
    )
    assert (
        isinstance(built.extension, RawPlutusData) and built.extension.data.tag == 121
    )

    basic_entry, fee_entry = (list(c) for c in built.constraints)
    assert basic_entry[0] == SundaeV4Deployment.for_network("preview").basic_order_hash
    assert isinstance(basic_entry[1], BasicSwap)
    assert fee_entry[0] == SundaeV4Deployment.for_network("preview").fee_constraint_hash
    assert fee_entry[1].data.tag == 121 and fee_entry[1].data.value == []

    swap = parse_basic_constraint(RawPlutusData(basic_entry[1].to_primitive()))
    assert swap.kind is BasicConstraintKind.SWAP
    asset, amount = list(list(swap.offered)[0])
    assert AssetClass.from_primitive(asset) == AssetClass(
        policy=bytes.fromhex(_STRW[:56]),
        asset_name=bytes.fromhex(_STRW[56:]),
    )
    assert amount == 2_000_000_000


def test_swap_datum_rejects_more_than_one_asset_per_side() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _leg().swap_datum(
            address_source=_address(_ADMIN_PAYMENT, _ADMIN_STAKE),
            in_assets=Assets(**{_STRW: 1, _MNGO: 1}),
            out_assets=Assets(**{_MINT: 1}),
        )


# ---------------------------------------------------------------------------
# deposit_datum: the basic deposit (constructor 0)
# ---------------------------------------------------------------------------


def test_deposit_datum_is_byte_exact_with_the_preview_deposit() -> None:
    reference = ORDERS["basic deposit MNGO+STRW -> LP (ctor 0)"]["datum"]
    built = _leg().deposit_datum(
        address_source=_address(_USER_PAYMENT, _USER_STAKE),
        offered=Assets(**{_MNGO: 100_000_000, _STRW: 100_000_000}),
        min_lp=Assets(**{_MNGO_STRW_LP: 190_520_397}),
        service_budget=3_000_000,
        max_per_execution=1_000_000,
    )
    assert built.to_cbor_hex() == reference
    deposit = parse_basic_constraint(
        RawPlutusData(list(built.constraints[0])[1].to_primitive())
    )
    assert isinstance(deposit, BasicDeposit) and len(deposit.offered) == 2


# ---------------------------------------------------------------------------
# strategy_datum
# ---------------------------------------------------------------------------


def _strategy() -> SundaeV4OrderDatum:
    return _leg().strategy_datum(
        address_source=_address(_USER_PAYMENT, _USER_STAKE),
        auth=MultisigSignature(key_hash=bytes.fromhex(_ADMIN_PAYMENT)),
        final_destinations=[_address(_USER_PAYMENT, _USER_STAKE)],
    )


def test_strategy_datum_carries_the_strategy_and_fee_constraints() -> None:
    built = _strategy()
    deployment = SundaeV4Deployment.for_network("preview")
    assert isinstance(built.destination, DestinationSelf)
    assert built.config_token == deployment.strategy_config_token
    strategy_entry, fee_entry = (list(c) for c in built.constraints)
    assert strategy_entry[0] == deployment.strategy_order_hash
    assert isinstance(strategy_entry[1], StrategyConstraint)
    assert strategy_entry[1].auth == MultisigSignature(
        key_hash=bytes.fromhex(_ADMIN_PAYMENT)
    )
    assert isinstance(list(strategy_entry[1].final_destinations)[0], DestinationFixed)
    assert fee_entry[0] == deployment.fee_constraint_hash
    assert fee_entry[1].data.tag == 121


def test_strategy_datum_round_trips_through_the_parser() -> None:
    built = _strategy()
    assert SundaeV4OrderDatum.from_cbor(built.to_cbor()).to_cbor() == built.to_cbor()


def test_signed_strategy_execution_round_trips() -> None:
    asset = AssetClass(
        policy=bytes.fromhex(_MINT[:56]), asset_name=bytes.fromhex(_MINT[56:])
    )
    execution = StrategyExecution(
        order_ref=OutputReference(transaction_id=bytes(32), output_index=1),
        validity_range=ValidityRange(
            lower_bound=IntervalBound(
                bound_type=IntervalBoundFinite(value=1_000), is_inclusive=BoolTrue()
            ),
            upper_bound=IntervalBound(
                bound_type=IntervalBoundPositiveInfinity(), is_inclusive=BoolTrue()
            ),
        ),
        min_deltas=IndefiniteList(
            [
                IndefiniteList([asset, 42]),
                IndefiniteList([AssetClass(policy=b"", asset_name=b""), -5_000_000]),
            ]
        ),
        final=OptionSomeInt(value=0),
        extension=RawPlutusData(CBORTag(121, [])),
    )
    signed = SignedStrategyExecution(
        execution=execution,
        signatures=IndefiniteList([IndefiniteList([bytes(32), bytes(64)])]),
    )
    reparsed = SignedStrategyExecution.from_cbor(signed.to_cbor())
    assert reparsed.to_cbor() == signed.to_cbor()
    assert list(list(reparsed.execution.min_deltas)[1])[1] == -5_000_000


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_redeemer_is_owner_cancel() -> None:
    redeemer = _SundaeV4CSState.cancel_redeemer()
    assert isinstance(redeemer, Redeemer)
    assert isinstance(redeemer.data, OrderCancel)
    assert redeemer.data.to_cbor_hex() == "d87980"


def test_order_owner_prefers_the_stake_key_and_falls_back_to_payment() -> None:
    with_stake = _SundaeV4CSState.order_owner(_address(_USER_PAYMENT, _USER_STAKE))
    assert with_stake == MultisigSignature(key_hash=bytes.fromhex(_USER_STAKE))
    enterprise = Address(
        payment_part=VerificationKeyHash(bytes.fromhex(_USER_PAYMENT)),
        network=Network.TESTNET,
    )
    assert _SundaeV4CSState.order_owner(enterprise) == MultisigSignature(
        key_hash=bytes.fromhex(_USER_PAYMENT)
    )
