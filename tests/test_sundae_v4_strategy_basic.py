"""Byte-exact parse + build validation of the SundaeSwap V4 strategy/basic orders.

The fixtures in ``sundae_v4_order_type_fixtures.json`` are real inline-datum CBOR
pulled from live order UTxOs at the V4 order validator on the Preview testnet
(public on-chain data): swap, strategy, and basic orders. They cover two bars:

* every order-type datum round trips byte-for-byte through the shared
  :class:`SundaeV4OrderDatum` parser (its ``constraints`` is opaque, so the shell
  is type-agnostic), and the typed :class:`StrategyConstraint` /
  :class:`BasicConstraint` payloads round trip byte-for-byte in isolation;
* the ``strategy_datum`` / ``basic_datum`` builders re-encode byte-for-byte to a
  live strategy / basic order.

The strategy / basic execution types (:class:`StrategyExecution` /
:class:`SignedStrategyExecution`) carry no live fixture — they ride in a scoop
redeemer, not an order datum — so they are validated by a self round trip for
parse-completeness only.
"""

import json
from pathlib import Path

from pycardano import Address
from pycardano import IndefiniteList
from pycardano import Network
from pycardano import RawPlutusData
from pycardano import VerificationKeyHash
from pycardano.serialization import CBORTag

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.sundae_v4 import BasicConstraint
from charli3_dendrite.dexs.amm.sundae_v4 import BoolTrue
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationFixed
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationSelf
from charli3_dendrite.dexs.amm.sundae_v4 import IntervalBound
from charli3_dendrite.dexs.amm.sundae_v4 import IntervalBoundFinite
from charli3_dendrite.dexs.amm.sundae_v4 import IntervalBoundPositiveInfinity
from charli3_dendrite.dexs.amm.sundae_v4 import MultisigSignature
from charli3_dendrite.dexs.amm.sundae_v4 import OptionSomeInt
from charli3_dendrite.dexs.amm.sundae_v4 import OutputReference
from charli3_dendrite.dexs.amm.sundae_v4 import SignedStrategyExecution
from charli3_dendrite.dexs.amm.sundae_v4 import StrategyConstraint
from charli3_dendrite.dexs.amm.sundae_v4 import StrategyExecution
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import ValidityRange
from charli3_dendrite.dexs.amm.sundae_v4 import _SundaeV4CPPState

FIXTURES = json.loads(
    (Path(__file__).parent / "sundae_v4_order_type_fixtures.json").read_text(),
)

# Applied (preview) order-constraint module hashes.
_STRATEGY_MODULE = "b298d0cb82fd8006d34e83d253e9250af4a35ac9af06573f94a48286"
_ROUTE_MODULE = "ef81595b5b8cf9bc5f0adfb0b8f3a2d60edef9d33755ca87fa86c077"
_FAIRNESS_MODULE = "b0df1c266988ab3bb5497bf9f6d8749a5f7726e88d43fca52efaa7f4"
_BASIC_MODULE = "3c1477d302e413f7fed7aff025dc65455d550c9a32409b17c7c6177d"
_STRATEGY_CONFIG_TOKEN = (
    "00d5ea9b8e3c4188cd6532351f716778e81be0ae4f932e5fd68f05aab6ed34ab"
)

# The order owner / destination of the reference orders (a base address: payment
# key + stake key); the strategy order's ``auth`` is the deployment admin key.
_OWNER_PAYMENT_VKH = "b4827ffb1a5f7a8aefb5c6f76cbd1d1db1975e24c478115a083749cb"
_OWNER_STAKE_VKH = "fa5e11b4390128b6b376ed7bab4b0ffa43afe7871408aab26bd7d67c"
_ADMIN_VKH = "e2afcadc7b111be7b89f283e9facffbbc5292f40fd13d7613e639c35"

# The basic reference orders' offered (USDr) and asked (USDM) assets.
_USDR_UNIT = "45df5f274b8950b512b08d10656864958659c4ecf3ffad092ef6302455534472"
_USDM_UNIT = "d8906ca5c7ba124a0407a32dab37b2c82b13b3dcd9111e42940dcea40014df105553444d"


def _all_datums() -> list[tuple[str, int, str]]:
    """``(kind, index, datum_hex)`` for every order-type fixture."""
    return [
        (kind, i, entry["datum_hex"])
        for kind in ("strategy", "basic", "swap")
        for i, entry in enumerate(FIXTURES[kind])
    ]


def _constraint_payload_cbor(datum_hex: str, module_hex: str) -> bytes:
    """The exact CBOR bytes of the constraint payload keyed by ``module_hex``.

    Walks the opaque order datum as raw plutus data and re-serialises the payload
    of the ``[module_hash, payload]`` entry whose hash matches, preserving the
    on-chain indefinite/definite array encoding.
    """
    raw = RawPlutusData.from_cbor(bytes.fromhex(datum_hex))
    constraints = list(raw.data.value[5])
    module = bytes.fromhex(module_hex)
    for entry in constraints:
        module_hash, payload = list(entry)
        if module_hash == module:
            return RawPlutusData(payload).to_cbor()
    msg = f"no constraint keyed by {module_hex}"
    raise AssertionError(msg)


def _leg() -> _SundaeV4CPPState:
    """A projected V4 constant-product leg (only the order builders are exercised)."""
    return _SundaeV4CPPState.model_validate(
        {
            "assets": Assets(**{"lovelace": 1_000_000, _USDR_UNIT: 500}),
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


def _owner_address() -> Address:
    return Address(
        payment_part=VerificationKeyHash(bytes.fromhex(_OWNER_PAYMENT_VKH)),
        staking_part=VerificationKeyHash(bytes.fromhex(_OWNER_STAKE_VKH)),
        network=Network.TESTNET,
    )


# ---------------------------------------------------------------------------
# (a) decode round trips
# ---------------------------------------------------------------------------


def test_all_order_type_datums_round_trip_byte_exact() -> None:
    """Every swap / strategy / basic order datum re-encodes byte-for-byte."""
    for kind, index, datum_hex in _all_datums():
        datum = SundaeV4OrderDatum.from_cbor(bytes.fromhex(datum_hex))
        assert datum.to_cbor().hex() == datum_hex, f"{kind}[{index}]"


def test_strategy_constraint_payload_round_trips_byte_exact() -> None:
    """The strategy constraint payload decodes to a typed record and re-encodes."""
    for index, entry in enumerate(FIXTURES["strategy"]):
        payload = _constraint_payload_cbor(entry["datum_hex"], _STRATEGY_MODULE)
        constraint = StrategyConstraint.from_cbor(payload)
        assert isinstance(constraint, StrategyConstraint)
        assert constraint.CONSTR_ID == 0
        assert isinstance(constraint.auth, MultisigSignature)
        assert isinstance(constraint.final_destinations, IndefiniteList)
        assert constraint.to_cbor().hex() == payload.hex(), f"strategy[{index}]"


def test_basic_constraint_payload_round_trips_byte_exact() -> None:
    """The basic constraint payload decodes to a typed record and re-encodes."""
    for index, entry in enumerate(FIXTURES["basic"]):
        payload = _constraint_payload_cbor(entry["datum_hex"], _BASIC_MODULE)
        constraint = BasicConstraint.from_cbor(payload)
        assert isinstance(constraint, BasicConstraint)
        # The basic order constraint tag is 0, not the swap order's 2.
        assert constraint.CONSTR_ID == 0
        assert isinstance(constraint.offered, IndefiniteList)
        assert isinstance(constraint.min_received, IndefiniteList)
        # ``offered`` is a List<(AssetClass, Int)>, not a single AssetClass; a
        # decoded opaque IndefiniteList yields each pair as raw plutus data.
        first_offered = list(list(constraint.offered)[0])
        assert len(first_offered) == 2
        assert isinstance(first_offered[1], int)
        assert constraint.to_cbor().hex() == payload.hex(), f"basic[{index}]"


# ---------------------------------------------------------------------------
# (b) build byte-exact vs live
# ---------------------------------------------------------------------------


def _build_strategy_datum() -> SundaeV4OrderDatum:
    return _leg().strategy_datum(
        address_source=_owner_address(),
        auth=MultisigSignature(key_hash=bytes.fromhex(_ADMIN_VKH)),
        final_destinations=[_owner_address()],
    )


def test_strategy_datum_is_byte_exact_with_live_order() -> None:
    """``strategy_datum`` re-encodes byte-for-byte to the live strategy order."""
    built = _build_strategy_datum().to_cbor().hex()
    assert built == FIXTURES["strategy"][0]["datum_hex"]


def test_strategy_datum_field_shape() -> None:
    """The strategy order's controlled fields carry the expected values."""
    datum = _build_strategy_datum()

    assert isinstance(datum.owner, MultisigSignature)
    assert datum.owner.key_hash == bytes.fromhex(_OWNER_PAYMENT_VKH)
    # A strategy order rests paying back to itself; the executor redirects.
    assert isinstance(datum.destination, DestinationSelf)
    assert datum.budget == 3_000_000
    assert datum.share_batcher == 10_000
    assert datum.config_token == bytes.fromhex(_STRATEGY_CONFIG_TOKEN)

    constraints = list(datum.constraints)
    assert len(constraints) == 3
    strategy_entry, route_entry, fairness_entry = (list(c) for c in constraints)
    assert strategy_entry[0] == bytes.fromhex(_STRATEGY_MODULE)
    assert route_entry[0] == bytes.fromhex(_ROUTE_MODULE)
    assert fairness_entry[0] == bytes.fromhex(_FAIRNESS_MODULE)
    # The route payload is the empty list; fairness is the empty constructor-0
    # record — both no-ops for an un-routed strategy order.
    assert route_entry[1] == []
    assert isinstance(fairness_entry[1], RawPlutusData)
    assert fairness_entry[1].data.tag == 121

    strategy = strategy_entry[1]
    assert isinstance(strategy, StrategyConstraint)
    assert isinstance(strategy.auth, MultisigSignature)
    assert strategy.auth.key_hash == bytes.fromhex(_ADMIN_VKH)
    destinations = list(strategy.final_destinations)
    assert len(destinations) == 1
    assert isinstance(destinations[0], DestinationFixed)


def _build_basic_datum(offered: int, asked: int) -> SundaeV4OrderDatum:
    return _leg().basic_datum(
        address_source=_owner_address(),
        in_assets=Assets(**{_USDR_UNIT: offered}),
        out_assets=Assets(**{_USDM_UNIT: asked}),
    )


def test_basic_datum_is_byte_exact_with_live_orders() -> None:
    """``basic_datum`` re-encodes byte-for-byte to two live basic orders."""
    # basic[0]: offered 6600 USDr, min received 1 USDM.
    assert _build_basic_datum(6600, 1).to_cbor().hex() == (
        FIXTURES["basic"][0]["datum_hex"]
    )
    # basic[1]: offered 660_000_000 USDr, min received 1 USDM.
    assert _build_basic_datum(660_000_000, 1).to_cbor().hex() == (
        FIXTURES["basic"][1]["datum_hex"]
    )


def test_basic_datum_field_shape() -> None:
    """The basic order's controlled fields carry the expected values."""
    datum = _build_basic_datum(6600, 1)

    assert isinstance(datum.owner, MultisigSignature)
    assert isinstance(datum.destination, DestinationFixed)
    assert datum.budget == 1_500_000
    assert datum.share_batcher == 500_000
    # A live basic order references the global settings entry (empty token).
    assert datum.config_token == b""

    constraints = list(datum.constraints)
    assert len(constraints) == 1
    basic_entry = list(constraints[0])
    assert basic_entry[0] == bytes.fromhex(_BASIC_MODULE)
    basic = basic_entry[1]
    assert isinstance(basic, BasicConstraint)

    offered = list(list(basic.offered)[0])
    assert offered[0] == AssetClass(
        policy=bytes.fromhex(_USDR_UNIT[:56]),
        asset_name=bytes.fromhex(_USDR_UNIT[56:]),
    )
    assert offered[1] == 6600
    asked = list(list(basic.min_received)[0])
    assert asked[0] == AssetClass(
        policy=bytes.fromhex(_USDM_UNIT[:56]),
        asset_name=bytes.fromhex(_USDM_UNIT[56:]),
    )
    assert asked[1] == 1


# ---------------------------------------------------------------------------
# strategy execution types (parse-completeness; no live order-datum fixture)
# ---------------------------------------------------------------------------


def _sample_signed_execution() -> SignedStrategyExecution:
    asset = AssetClass(
        policy=bytes.fromhex(_USDM_UNIT[:56]),
        asset_name=bytes.fromhex(_USDM_UNIT[56:]),
    )
    execution = StrategyExecution(
        order_ref=OutputReference(transaction_id=bytes(32), output_index=1),
        validity_range=ValidityRange(
            lower_bound=IntervalBound(
                bound_type=IntervalBoundFinite(value=1_000),
                is_inclusive=BoolTrue(),
            ),
            upper_bound=IntervalBound(
                bound_type=IntervalBoundPositiveInfinity(),
                is_inclusive=BoolTrue(),
            ),
        ),
        min_received=IndefiniteList([IndefiniteList([asset, 42])]),
        final=OptionSomeInt(value=0),
        extension=RawPlutusData(CBORTag(121, [])),
    )
    return SignedStrategyExecution(
        execution=execution,
        signatures=IndefiniteList([IndefiniteList([bytes(32), bytes(64)])]),
    )


def test_signed_strategy_execution_round_trips() -> None:
    """A built signed strategy execution parses back to itself (parse-complete)."""
    built = _sample_signed_execution()
    cbor = built.to_cbor()
    reparsed = SignedStrategyExecution.from_cbor(cbor)
    assert isinstance(reparsed, SignedStrategyExecution)
    assert reparsed.to_cbor() == cbor

    execution = reparsed.execution
    assert isinstance(execution, StrategyExecution)
    assert isinstance(execution.final, OptionSomeInt)
    assert execution.final.value == 0
    assert isinstance(execution.validity_range, ValidityRange)
    assert isinstance(
        execution.validity_range.upper_bound.bound_type,
        IntervalBoundPositiveInfinity,
    )
