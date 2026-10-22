"""The shared backend-shaped record builder behind the protocol fixture helpers."""

from charli3_dendrite.dataclasses.models import Assets
from tests.lending import records_common
from tests.lending.fluidtokens_v4 import records as ft_records
from tests.lending.liqwid import records as liqwid_records
from tests.lending.liqwid import test_oracles
from tests.lending.records_common import BLOCK_INDEX
from tests.lending.records_common import BLOCK_TIME
from tests.lending.records_common import record_info

ENTRY = {
    "out_ref": "ab" * 32 + "#3",
    "address": "addr1example",
    "datum_cbor": "d87980",
    "assets": {"lovelace": 2_000_000, "cc" * 28 + "4142": 7},
}


def test_both_protocol_helpers_use_the_shared_builder():
    assert ft_records.record_info is records_common.record_info
    assert liqwid_records.record_info is records_common.record_info


def test_an_entry_without_chain_position_gets_the_default_block():
    info = record_info(ENTRY)
    assert (BLOCK_TIME, BLOCK_INDEX) == (1_790_000_000, 0)
    assert info.model_dump() == {
        "address": "addr1example",
        "tx_hash": "ab" * 32,
        "tx_index": 3,
        "block_time": BLOCK_TIME,
        "block_index": BLOCK_INDEX,
        "block_hash": "",
        "datum_hash": "",
        "datum_cbor": "d87980",
        "assets": Assets(root=dict(ENTRY["assets"])).model_dump(),
        "plutus_v2": False,
    }


def test_an_entry_with_chain_position_keeps_it():
    info = record_info({**ENTRY, "block_time": 1_700_000_123, "block_index": 9})
    assert (info.block_time, info.block_index) == (1_700_000_123, 9)


def test_overrides_win_over_the_entry():
    info = record_info(
        {**ENTRY, "block_time": 5, "block_index": 6},
        block_time=1,
        datum_cbor="",
    )
    assert (info.block_time, info.block_index, info.datum_cbor) == (1, 6, "")


def test_feed_info_uses_the_shared_default_block():
    info = test_oracles.feed_info(ENTRY["out_ref"], "d87980")
    assert (info.block_time, info.block_index) == (BLOCK_TIME, BLOCK_INDEX)
