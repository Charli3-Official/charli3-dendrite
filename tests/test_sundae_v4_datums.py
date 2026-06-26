"""Byte-exact parsing tests for the SundaeSwap V4 datum / redeemer types.

The fixtures in ``sundae_v4_preview_fixtures.json`` are real inline-datum and
redeemer CBOR pulled from the deployed V4 contracts on the Preview testnet (public
on-chain data). Each pool datum is asserted to round trip byte-for-byte; the order
datums and the scoop redeemers are decoded and their structure is asserted.
"""

import json
from pathlib import Path

import pytest
from cbor2 import CBORTag
from pycardano import IndefiniteList
from pycardano import RawPlutusData

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dexs.amm.sundae_v4 import BountyClaim
from charli3_dendrite.dexs.amm.sundae_v4 import ConstantSumConfig
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationFixed
from charli3_dendrite.dexs.amm.sundae_v4 import DestinationSelf
from charli3_dendrite.dexs.amm.sundae_v4 import MultisigSignature
from charli3_dendrite.dexs.amm.sundae_v4 import OrderScoop
from charli3_dendrite.dexs.amm.sundae_v4 import PoolAction
from charli3_dendrite.dexs.amm.sundae_v4 import Rational
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4PoolDatum
from charli3_dendrite.dexs.amm.sundae_v4 import TranscriptEntry

FIXTURES = json.loads(
    (Path(__file__).parent / "sundae_v4_preview_fixtures.json").read_text(),
)


def _constr(raw):
    """Return the constructor index of a decoded raw plutus value."""
    if isinstance(raw, RawPlutusData):
        tag = raw.data.tag
    elif isinstance(raw, CBORTag):
        tag = raw.tag
    else:
        return None
    # Tags 121..127 map to constructors 0..6; 1280+ for 7..127.
    if 121 <= tag <= 127:
        return tag - 121
    if tag == 102:
        return None
    return tag - 1280 + 7


# ---------------------------------------------------------------------------
# Pool datums: byte-exact round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", range(len(FIXTURES["pool_datums"])))
def test_pool_datum_byte_exact(index):
    """Every pool datum decodes and re-encodes to the exact original bytes."""
    original = FIXTURES["pool_datums"][index]["datum_hex"]

    datum = SundaeV4PoolDatum.from_cbor(original)
    assert datum.to_cbor_hex() == original

    # The deployed pools are 3-asset constant-sum vaults.
    assert len(datum.assets) == 3
    for pair in datum.assets:
        assert len(pair) == 2
        asset_class, reserve = pair[0], pair[1]
        assert AssetClass.from_primitive(asset_class) is not None
        assert isinstance(reserve, int)
        assert reserve > 0

    assert isinstance(datum.total_lp, int)
    assert isinstance(datum.circulating_lp, int)
    assert isinstance(datum.preminted_lp, int)
    assert len(datum.identifier) == 28

    # At least the scoop action (tag 3) must be installed and enabled.
    assert len(datum.actions) >= 1
    scoop = next(a for a in datum.actions if a.tag == 3)
    assert type(scoop.enabled).__name__ == "BoolTrue"
    assert len(scoop.modules) == 3
    for module_hash in scoop.modules:
        assert isinstance(module_hash, bytes)
        assert len(module_hash) == 28

    # module_state is a list of [module_hash, config_hash] pairs.
    assert len(datum.module_state) >= 3
    for entry in datum.module_state:
        assert len(entry) == 2
        assert isinstance(entry[0], bytes) and len(entry[0]) == 28


def test_pool_datum_assets_roundtrip_to_asset_class():
    """The bare-tuple assets decode into reusable AssetClass values."""
    datum = SundaeV4PoolDatum.from_cbor(FIXTURES["pool_datums"][0]["datum_hex"])
    units = []
    for asset_class, _reserve in datum.assets:
        ac = AssetClass.from_primitive(asset_class)
        units.append(ac.policy.hex() + ac.asset_name.hex())
    # Three distinct on-chain units in the constant-sum basket.
    assert len(set(units)) == 3


# ---------------------------------------------------------------------------
# Order datums: decode + structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", range(len(FIXTURES["order_datums"])))
def test_order_datum_structure(index):
    """Every order datum decodes, round trips byte-exact and has 7 fields."""
    original = FIXTURES["order_datums"][index]["datum_hex"]

    order = SundaeV4OrderDatum.from_cbor(original)
    assert order.to_cbor_hex() == original

    # Owner is a single-signature multisig in all deployed fixtures.
    assert isinstance(order.owner, MultisigSignature)
    assert len(order.owner.key_hash) == 28

    assert isinstance(order.destination, (DestinationFixed, DestinationSelf))
    assert order.budget == 3_000_000
    assert order.share_batcher == 10_000
    assert len(order.config_token) == 32

    # constraints is a keyed list of [constraint_module_hash, payload] entries.
    assert len(order.constraints) == 3
    for entry in order.constraints:
        assert len(entry) == 2
        assert isinstance(entry[0], bytes) and len(entry[0]) == 28


def test_order_datum_swap_constraint_payload():
    """A swap order carries a tag-2 (Swap) constraint payload."""
    # Order index 1 is the swap-config order (DestinationFixed, swap_order hash).
    order = SundaeV4OrderDatum.from_cbor(FIXTURES["order_datums"][1]["datum_hex"])
    assert isinstance(order.destination, DestinationFixed)

    # Find the swap constraint payload (inner constructor index 2 == Swap; the
    # keyed constraint payloads are preserved as raw CBOR tags).
    swap_payloads = [
        payload
        for _hash, payload in order.constraints
        if isinstance(payload, CBORTag) and _constr(payload) == 2
    ]
    assert len(swap_payloads) == 1
    swap = swap_payloads[0]
    # Swap payload: offered AssetClass, original_offered, remaining_offered,
    # min_received list-of-(AssetClass, Int).
    fields = list(swap.value)
    assert len(fields) == 4
    offered = AssetClass.from_primitive(fields[0])
    assert offered.policy.hex() + offered.asset_name.hex()
    assert isinstance(fields[1], int) and fields[1] > 0
    assert isinstance(fields[2], int) and fields[2] > 0
    assert len(fields[3]) >= 1  # min_received entries


# ---------------------------------------------------------------------------
# Redeemers: the pool Action transcript and the order Scoop redeemer
# ---------------------------------------------------------------------------


def test_pool_action_redeemer():
    """The pool spend Action redeemer decodes with a non-empty transcript."""
    # Redeemer 0 is the pool Action; the others are order Scoop redeemers.
    redeemer = PoolAction.from_cbor(FIXTURES["pool_spend_redeemers_hex"][0])
    assert redeemer.to_cbor_hex() == FIXTURES["pool_spend_redeemers_hex"][0]

    assert redeemer.tag == 3
    assert redeemer.pool_input_index == 2
    assert redeemer.pool_output_index == 0
    assert len(redeemer.transcript) >= 1

    entry = redeemer.transcript[0]
    assert isinstance(entry, TranscriptEntry)
    assert len(entry.state_after.assets) == 3
    assert entry.state_after.total_lp > 0
    # operation_tag 5 == constant-sum swap+claim; operation_data is a bounty claim.
    assert entry.operation_tag == 5
    claim = BountyClaim.from_cbor(entry.operation_data.to_cbor_hex())
    assert isinstance(claim.asset, AssetClass)
    assert claim.amount > 0


@pytest.mark.parametrize(
    ("index", "expected_input_index"),
    [(1, 0), (2, 1)],
)
def test_order_scoop_redeemer(index, expected_input_index):
    """The order scoop redeemers decode to OrderScoop with the own input index."""
    original = FIXTURES["pool_spend_redeemers_hex"][index]
    redeemer = OrderScoop.from_cbor(original)
    assert redeemer.to_cbor_hex() == original
    assert redeemer.own_input_index == expected_input_index


# ---------------------------------------------------------------------------
# Config types: shape / encoding sanity
# ---------------------------------------------------------------------------


def test_constant_sum_config_roundtrip():
    """A constructed constant-sum config round trips through CBOR."""
    config = ConstantSumConfig(
        prices=IndefiniteList([1, 1, 1]),
        fee=Rational(num=8, den=1000),
        bounty_k=Rational(num=9, den=4000),
    )
    restored = ConstantSumConfig.from_cbor(config.to_cbor_hex())
    assert list(restored.prices) == [1, 1, 1]
    assert restored.fee.num == 8
    assert restored.fee.den == 1000
    assert restored.bounty_k.num == 9
    assert restored.bounty_k.den == 4000
