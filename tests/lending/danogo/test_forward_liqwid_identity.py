"""Danogo's Liqwid oracle leaves are identified by the priced asset alone.

The fixtures are the captured Liqwid V2 oracle outputs (``tests/lending/liqwid``),
read through an address-keyed backend, against the handles of the packaged
``recipes.json``. Those handles were captured as ``<updater>:<asset>`` under an
updater key that has since rotated.
"""

from __future__ import annotations

import dataclasses

import pytest

from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.danogo.oracles.forward import leaf_discriminator
from charli3_dendrite.lending.danogo.oracles.forward import leaf_matches_handle
from charli3_dendrite.lending.danogo.oracles.forward import resolve_leaf_info
from charli3_dendrite.lending.danogo.oracles.locator import LeafHandle
from charli3_dendrite.lending.danogo.oracles.locator import load_registry
from charli3_dendrite.lending.danogo.transactions._common import _leaf_for_handle
from charli3_dendrite.lending.danogo.transactions.context import _utxo_from_info
from charli3_dendrite.lending.liqwid.datums import POLICY_KEY_PREFIX
from charli3_dendrite.lending.liqwid.datums import decode_oracle_feed
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import liqwid_selectors
from tests.lending.liqwid.records import records_of
from tests.lending.records_common import record_info

LIQWID = "TLIQWID_ORACLE_V2"


class _AddressBackend:
    """Serves unspent outputs by address, optionally filtered by held assets."""

    def __init__(self, infos: list[PoolStateInfo]) -> None:
        self.infos = infos
        self.queried: list[str] = []

    def get_pool_utxos(
        self,
        addresses,
        assets=None,
        limit=1000,
        page=0,
        historical=True,
    ):
        self.queried.extend(addresses)
        return [
            info
            for info in self.infos
            if info.address in addresses
            and all(asset in info.assets.root for asset in assets or ())
        ]


def _liqwid_handles() -> list[LeafHandle]:
    """Every distinct Liqwid oracle handle of the packaged recipes."""
    handles = {
        step.handle
        for recipe in load_registry().values()
        for step in recipe.steps
        if step.handle.otype == LIQWID
    }
    return sorted(handles, key=lambda handle: handle.datum_key)


def _live_infos() -> list[PoolStateInfo]:
    """Every captured oracle output live at the snapshot, decoys included."""
    return [record_info(rec) for rec in records_of("oracle", live=True)]


def _token(handle: LeafHandle) -> str:
    return handle.nft_policy + handle.nft_name


def _live_feeds(token: str) -> list[PoolStateInfo]:
    """Live outputs holding the oracle token (the genuine feeds)."""
    return [info for info in _live_infos() if token in info.assets.root]


def _asset_key(info: PoolStateInfo) -> str:
    return decode_oracle_feed(info.datum_cbor).asset_key


@pytest.fixture(scope="module")
def handles() -> list[LeafHandle]:
    found = _liqwid_handles()
    assert len(found) == 3  # ADA, DJED and SHEN
    return found


def test_a_liqwid_leaf_is_keyed_by_its_asset_key_alone():
    # Every captured feed, decoys and the policy-wide feed included.
    keyed = 0
    for rec in records_of("oracle"):
        try:
            feed = decode_oracle_feed(rec["datum_cbor"])
        except ValueError:
            assert leaf_discriminator(LIQWID, rec["datum_cbor"]) == ""
            continue
        assert leaf_discriminator(LIQWID, rec["datum_cbor"]) == feed.asset_key
        keyed += 1
    assert keyed


def test_live_feed_keys_are_unique_and_ada_is_matched_once(handles):
    token = _token(handles[0])
    keys = [leaf_discriminator(LIQWID, info.datum_cbor) for info in _live_feeds(token)]
    assert len(keys) == len(set(keys))
    assert keys.count("lovelace") == 1
    policy_wide = [key for key in keys if key.startswith(POLICY_KEY_PREFIX)]
    assert len(policy_wide) == 1  # the live policy-wide feed is not "lovelace"


def test_a_captured_handle_matches_on_its_asset_part(handles):
    ada = next(handle for handle in handles if handle.datum_key.endswith(":lovelace"))
    feeds = _live_feeds(_token(ada))
    ada_feed = next(info for info in feeds if _asset_key(info) == "lovelace")
    policy_feed = next(
        info for info in feeds if _asset_key(info).startswith(POLICY_KEY_PREFIX)
    )
    # The captured updater no longer signs the ADA feed.
    assert decode_oracle_feed(ada_feed.datum_cbor).updater != ada.datum_key[:56]
    assert leaf_matches_handle(ada, ada_feed.datum_cbor)
    assert not leaf_matches_handle(ada, policy_feed.datum_cbor)
    current = dataclasses.replace(ada, datum_key="lovelace")
    assert leaf_matches_handle(current, ada_feed.datum_cbor)
    by_policy = dataclasses.replace(ada, datum_key=_asset_key(policy_feed))
    assert leaf_matches_handle(by_policy, policy_feed.datum_cbor)
    assert not leaf_matches_handle(by_policy, ada_feed.datum_cbor)
    assert not leaf_matches_handle(ada, None)


def test_a_handle_without_a_datum_key_matches_any_datum():
    handle = LeafHandle(otype=LIQWID, address="a", nft_policy="p", nft_name="")
    assert leaf_matches_handle(handle, None)


def test_other_kinds_compare_the_whole_datum_key():
    minswap = LeafHandle(
        otype="TMINSWAP_LP",
        address="a",
        nft_policy="p",
        nft_name="",
        datum_key="x:lovelace",
    )
    # Only Liqwid oracle keys carry an earlier updater prefix.
    assert not leaf_matches_handle(minswap, None)


def test_every_recipe_handle_resolves_to_its_assets_live_feed(handles):
    backend = _AddressBackend(_live_infos())
    for handle in handles:
        info = resolve_leaf_info(backend, handle)
        assert info is not None, handle.datum_key
        assert _asset_key(info) == handle.datum_key.rpartition(":")[2]
        assert _token(handle) in info.assets.root


def test_the_snapshot_leaf_matcher_follows_the_same_rule(handles):
    leaves = [_utxo_from_info(info) for info in _live_feeds(_token(handles[0]))]
    for handle in handles:
        leaf = _leaf_for_handle(leaves, handle)
        assert leaf is not None, handle.datum_key
        assert decode_oracle_feed(leaf.datum).asset_key == (
            handle.datum_key.rpartition(":")[2]
        )


def test_the_snapshot_leaf_matcher_refuses_two_matches(handles):
    leaves = [_utxo_from_info(info) for info in _live_feeds(_token(handles[0]))]
    ada = next(handle for handle in handles if handle.datum_key.endswith(":lovelace"))
    ada_leaf = _leaf_for_handle(leaves, ada)
    assert ada_leaf is not None
    assert _leaf_for_handle([*leaves, dataclasses.replace(ada_leaf)], ada) is None


def test_every_live_liqwid_feed_resolves_whatever_its_validator(handles):
    # A handle pins the oracle token and the asset; the recipe's address is not a
    # filter, so the pegs feeds at the other oracle validator resolve too.
    pinned = handles[0].address
    feeds = _live_feeds(_token(handles[0]))
    assert {info.address for info in feeds} - {pinned}
    backend = _AddressBackend(_live_infos())
    for feed in feeds:
        handle = dataclasses.replace(handles[0], datum_key=_asset_key(feed))
        info = resolve_leaf_info(backend, handle)
        assert info is not None, handle.datum_key
        assert (info.tx_hash, info.tx_index) == (feed.tx_hash, feed.tx_index)


def test_a_liqwid_handle_reads_every_oracle_validator(handles):
    backend = _AddressBackend([])
    assert resolve_leaf_info(backend, handles[0]) is None
    oracle_addresses = {
        selector.address
        for selector in liqwid_selectors()
        if selector.kind is LiqwidKind.ORACLE
    }
    assert set(backend.queried) == oracle_addresses
    assert handles[0].address in oracle_addresses


def test_other_kinds_read_their_pinned_address():
    backend = _AddressBackend([])
    handle = LeafHandle(
        otype="TMINSWAP_LP", address="addr", nft_policy="p", nft_name=""
    )
    assert resolve_leaf_info(backend, handle) is None
    assert backend.queried == ["addr"]
