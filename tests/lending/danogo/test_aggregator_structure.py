"""Lock the verified structural facts of the Float oracle-config datum.

These assert ONLY what is confirmed from the live mainnet packed bytes (not the
still-pending tag semantics / hop sub-structure):

1. ``price_paths`` entries reference ``oracle_sources`` by index: every trailing byte
   after each entry's 28-byte token id stays within ``0..len(oracle_sources)-1``, and
   the maximum referenced index equals ``len(oracle_sources) - 1`` (the last source is
   reachable). This is what pins field1=price_paths / field2=oracle_sources.
2. ``oracle_sources`` is a tagged union: a 1-byte tag followed by a tag-consistent
   fixed-length locator payload.
"""

import json
from collections import defaultdict
from pathlib import Path

from charli3_dendrite.lending.danogo.oracles.aggregator_datums import OraclePathDatum
from charli3_dendrite.lending.danogo.oracles.aggregator_datums import (
    decode_price_path_entry,
)
from charli3_dendrite.lending.danogo.oracles.redeemer import CalcType

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mainnet_datums.json").read_text()
)
_TOKEN_ID_LEN = 28


def _first_path():
    return OraclePathDatum.from_cbor(FIXTURES["oracle_paths"][0]["datum"])


def _all_configs():
    return [OraclePathDatum.from_cbor(c["datum"]) for c in FIXTURES["oracle_paths"]]


def test_price_paths_reference_oracle_sources_by_index():
    d = _first_path()
    n_sources = len(d.oracle_sources)
    max_ref = 0
    for entry in d.price_paths:
        trailing = entry[_TOKEN_ID_LEN:]
        assert trailing, "price_path entry should carry hop bytes after token id"
        for b in trailing:
            assert b <= n_sources - 1, f"trailing byte {b} exceeds source index range"
            max_ref = max(max_ref, b)
    # The highest referenced index hits the last source -> field2 is the source list.
    assert max_ref == n_sources - 1


def test_oracle_sources_is_tagged_union_with_consistent_payload_len():
    d = _first_path()
    lengths_by_tag: dict[int, set[int]] = defaultdict(set)
    for entry in d.oracle_sources:
        assert len(entry) >= 1
        tag = entry[0]
        lengths_by_tag[tag].add(len(entry) - 1)
    # Every tag uses a single, fixed locator-payload length.
    for tag, lengths in lengths_by_tag.items():
        assert len(lengths) == 1, f"tag {tag} has varying payload lengths {lengths}"
    # Two-id locators (e.g. Liqwid qToken: market_state + market_param) are present.
    all_lengths = {next(iter(s)) for s in lengths_by_tag.values()}
    assert 2 * _TOKEN_ID_LEN in all_lengths


def test_every_price_path_entry_decodes_under_the_grammar():
    # token(28) calc_type(1) [ n_hops(1) (src,rev,scale){n_hops} ]+ -- must decode
    # cleanly (clean bool is_reverse, in-range source indices) for EVERY entry of
    # EVERY live config datum.
    n_entries = 0
    for cfg in _all_configs():
        n_src = len(cfg.oracle_sources)
        for entry in cfg.price_paths:
            decoded = decode_price_path_entry(entry, n_src)
            n_entries += 1
            assert decoded.calc_type in (CalcType.NORMAL, CalcType.SPLASH)
            assert 1 <= len(decoded.paths) <= 3  # alternative derivation paths
            for path in decoded.paths:
                assert 1 <= len(path) <= 4
                for hop in path:
                    assert 0 <= hop.source_index <= n_src - 1
                    assert isinstance(hop.is_reverse, bool)
    assert n_entries >= 80  # all three configs combined


def test_hop_scale_exp_tracks_source_kind():
    # The per-hop scale exponent is determined by the referenced source's tag: tag 7
    # is always 3, tags 2/3/4 are always 0; only tag 8 varies per-instance.
    scales_by_tag: dict[int, set[int]] = defaultdict(set)
    for cfg in _all_configs():
        tags = [s[0] for s in cfg.oracle_sources]
        for decoded in cfg.decode_paths():
            for path in decoded.paths:
                for hop in path:
                    scales_by_tag[tags[hop.source_index]].add(hop.scale_exp)
    assert scales_by_tag[7] == {3}
    assert scales_by_tag[2] == {0}
    assert scales_by_tag[3] == {0}
    assert scales_by_tag[4] == {0}


def test_decode_paths_round_trips_token_ids():
    cfg = _first_path()
    by_token = cfg.paths_by_token()
    # Every packed entry's leading 28 bytes is recovered as the token key.
    assert len(by_token) == len(cfg.price_paths)
    for entry in cfg.price_paths:
        assert entry[:_TOKEN_ID_LEN].hex() in by_token
