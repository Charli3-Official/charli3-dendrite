import json
from decimal import Decimal
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.orcfax import ORCFAX_TOLERANCE_MS
from charli3_dendrite.lending.oracles.orcfax import OrcfaxFeedDatum
from charli3_dendrite.lending.oracles.orcfax import OrcfaxResolver

FIX = json.loads((Path(__file__).parent / "fixtures" / "orcfax_feed.json").read_text())


def _info(cbor, unit):
    return PoolStateInfo(
        address=FIX["feed_address"],
        tx_hash="ab",
        tx_index=0,
        block_time=1,
        block_index=1,
        block_hash="cd",
        datum_hash="00",
        datum_cbor=cbor,
        assets=Assets(root={"lovelace": 2_000_000, unit: 1}),
        plutus_v2=True,
    )


def test_orcfax_datum_parses():
    datum = OrcfaxFeedDatum.from_cbor(FIX["datum_cbor"])
    assert datum.statement.feed_id.decode() == FIX["feed_id"]
    assert datum.statement.created_at == FIX["created_at"]
    assert datum.statement.body.num == FIX["num"]
    assert datum.statement.body.denom == FIX["denom"]
    assert datum.statement.body.denom > 0


def test_orcfax_resolver_returns_positive_price():
    ref = OracleRef(
        source=OracleSource.ORCFAX,
        token="adausd",
        feed_policy=FIX["feed_nft"][:56],
        feed_name=FIX["feed_nft"][56:],
        address=FIX["feed_address"],
        feed_id=FIX["feed_id_prefix"],
    )
    utxos = PoolStateList(root=[_info(FIX["datum_cbor"], FIX["feed_nft"])])
    price = OrcfaxResolver().resolve(ref, utxos)
    assert price is not None
    assert price.source is OracleSource.ORCFAX
    assert price.num == FIX["num"]
    assert price.denom == FIX["denom"]
    assert Decimal("0.01") < price.as_decimal() < Decimal("100")
    assert price.valid_from == FIX["created_at"]
    assert price.valid_to == FIX["created_at"] + ORCFAX_TOLERANCE_MS


def test_orcfax_resolver_filters_by_feed_id():
    # A sibling feed (ADA-USDA) at the same policy/address must NOT match
    # an ADA-USD ref, because they share the feed-NFT unit.
    usda_cbor = (
        "d8799fd8799f4e4345522f4144412d555344412f331b0000019edc9b6589"
        "d8799f1a613a5adb1b00000002540be400ffffd8799f581c3c12f6735ef8"
        "7655c5b27bced3f828d857d0a27fd20f2cda18ebf2fbffff"
    )
    ref = OracleRef(
        source=OracleSource.ORCFAX,
        token="adausd",
        feed_policy=FIX["feed_nft"][:56],
        feed_name=FIX["feed_nft"][56:],
        address=FIX["feed_address"],
        feed_id=FIX["feed_id_prefix"],
    )
    utxos = PoolStateList(root=[_info(usda_cbor, FIX["feed_nft"])])
    assert OrcfaxResolver().resolve(ref, utxos) is None


def _ref():
    return OracleRef(
        source=OracleSource.ORCFAX,
        token="adausd",
        feed_policy=FIX["feed_nft"][:56],
        feed_name=FIX["feed_nft"][56:],
        address=FIX["feed_address"],
        feed_id=FIX["feed_id_prefix"],
    )


def _datum_with(num, denom):
    # Reuse the real fixture (incl. its opaque context) and only swap the body.
    datum = OrcfaxFeedDatum.from_cbor(FIX["datum_cbor"])
    datum.statement.body.num = num
    datum.statement.body.denom = denom
    return datum.to_cbor_hex()


def _datum_at(num, denom, created_at):
    # Reuse the real fixture but swap both the body and the creation time.
    datum = OrcfaxFeedDatum.from_cbor(FIX["datum_cbor"])
    datum.statement.body.num = num
    datum.statement.body.denom = denom
    datum.statement.created_at = created_at
    return datum.to_cbor_hex()


def test_orcfax_resolver_picks_freshest_fact_statement():
    # Two valid ADA-USD statements differing only in `created_at`: the resolver
    # must return the price from the newer one regardless of UTxO order, because
    # stale fact-statement UTxOs may remain unspent at the FS address.
    older = FIX["created_at"]
    newer = FIX["created_at"] + 3_600_000  # +1h
    old_cbor = _datum_at(100_000, 1_000_000, older)
    new_cbor = _datum_at(163_333, 1_000_000, newer)

    forward = PoolStateList(
        root=[_info(old_cbor, FIX["feed_nft"]), _info(new_cbor, FIX["feed_nft"])]
    )
    price = OrcfaxResolver().resolve(_ref(), forward)
    assert price is not None
    assert price.num == 163_333
    assert price.valid_from == newer
    assert price.valid_to == newer + ORCFAX_TOLERANCE_MS

    # Reversed order (newest first) yields the same freshest result.
    reverse = PoolStateList(
        root=[_info(new_cbor, FIX["feed_nft"]), _info(old_cbor, FIX["feed_nft"])]
    )
    price_rev = OrcfaxResolver().resolve(_ref(), reverse)
    assert price_rev is not None
    assert price_rev.num == 163_333
    assert price_rev.valid_from == newer


def test_orcfax_skips_undecodable_datum():
    # A UTxO carrying the FS token but a non-Orcfax datum must not crash.
    utxos = PoolStateList(root=[_info("d87980", FIX["feed_nft"])])
    assert OrcfaxResolver().resolve(_ref(), utxos) is None


def test_orcfax_skips_zero_denominator():
    utxos = PoolStateList(root=[_info(_datum_with(163333, 0), FIX["feed_nft"])])
    assert OrcfaxResolver().resolve(_ref(), utxos) is None


def test_orcfax_skips_non_positive_num():
    utxos = PoolStateList(root=[_info(_datum_with(0, 1000000), FIX["feed_nft"])])
    assert OrcfaxResolver().resolve(_ref(), utxos) is None


def test_orcfax_continues_past_bad_datum_to_real_price():
    # A bad datum preceding the real fixture must not block resolution.
    utxos = PoolStateList(
        root=[
            _info("d87980", FIX["feed_nft"]),
            _info(_datum_with(163333, 0), FIX["feed_nft"]),
            _info(FIX["datum_cbor"], FIX["feed_nft"]),
        ]
    )
    price = OrcfaxResolver().resolve(_ref(), utxos)
    assert price is not None
    assert price.num == FIX["num"]
    assert price.denom == FIX["denom"]


def test_orcfax_rejects_unanchored_version_suffix():
    # `CER/ADA-USD/3` must not false-accept `CER/ADA-USD/30`.
    bogus = _datum_with(163333, 1000000)
    datum = OrcfaxFeedDatum.from_cbor(bogus)
    datum.statement.feed_id = b"CER/ADA-USD/30"
    utxos = PoolStateList(root=[_info(datum.to_cbor_hex(), FIX["feed_nft"])])
    assert OrcfaxResolver().resolve(_ref(), utxos) is None
