"""leaf_rate dispatches to the verified per-source parsers."""
import json
from fractions import Fraction
from pathlib import Path

from charli3_dendrite.lending.danogo.oracles.forward import leaf_discriminator
from charli3_dendrite.lending.danogo.oracles.forward import leaf_rate
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.oracles.reproduce import leaf_forward_rates
from charli3_dendrite.lending.liqwid.datums import decode_oracle_feed
from tests.lending.liqwid.records import records_of

REPRO = json.loads(
    (Path(__file__).parent / "fixtures" / "redeemer_reproduction.json").read_text()
)
# Each captured Liqwid feed's priced asset, price and updater as the field readers
# returned them.
LIQWID_FEEDS = json.loads(
    (
        Path(__file__).parents[1] / "liqwid" / "fixtures" / "legacy_reader_outputs.json"
    ).read_text(),
)["oracle"]


def test_liqwid_oracle_v2_leaf_parses_to_positive_rate():
    leaf = next(
        lf for e in REPRO for lf in e["leaves"] if lf["otype"] == "TLIQWID_ORACLE_V2"
    )
    rate = leaf_rate(
        leaf["otype"],
        leaf["datum"],
        tuple((p, n, int(q)) for p, n, q in leaf["assets"]),
        quote_unit="lovelace",
    )
    assert rate is not None and rate > 0


def test_unparseable_leaf_returns_none():
    assert leaf_rate("TLIQWID_ORACLE_V2", None, (), quote_unit="lovelace") is None


def test_liqwid_feed_leaves_keep_their_rate_and_are_keyed_by_asset():
    # Every captured Liqwid feed. The legacy readers' "lovelace" for a policy-wide
    # feed is now its policy key; every other key is the legacy asset.
    feeds = records_of("oracle")
    assert {rec["out_ref"] for rec in feeds} == set(LIQWID_FEEDS)
    for rec in feeds:
        expected = LIQWID_FEEDS[rec["out_ref"]]
        datum = rec["datum_cbor"]
        leaf = OracleLeaf(otype=OracleUtxoType.TLIQWID_ORACLE_V2, datum=datum)
        assert leaf_forward_rates(leaf) == [("liqv2", Fraction(*expected["price"]))]
        feed = decode_oracle_feed(datum)
        assert leaf_discriminator("TLIQWID_ORACLE_V2", datum) == feed.asset_key
        if not feed.policy_wide:
            assert feed.asset_key == expected["asset"]
