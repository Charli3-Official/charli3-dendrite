import json
from decimal import Decimal
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.charli3 import Charli3OracleDatum
from charli3_dendrite.lending.oracles.charli3 import Charli3Resolver
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource

FIX = json.loads((Path(__file__).parent / "fixtures" / "charli3_feed.json").read_text())


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


def test_charli3_datum_parses():
    datum = Charli3OracleDatum.from_cbor(FIX["datum_cbor"])
    assert datum.price_data.price_map[0] > 0  # price


def test_charli3_resolver_returns_positive_price():
    ref = OracleRef(
        source=OracleSource.CHARLI3,
        token="adausd",
        feed_policy=FIX["feed_nft"][:56],
        feed_name=FIX["feed_nft"][56:],
        address=FIX["feed_address"],
        decimals=FIX["decimals"],
    )
    utxos = PoolStateList(root=[_info(FIX["datum_cbor"], FIX["feed_nft"])])
    price = Charli3Resolver().resolve(ref, utxos)
    assert price is not None
    assert price.source is OracleSource.CHARLI3
    assert price.denom == 10 ** FIX["decimals"]
    assert Decimal("0.01") < price.as_decimal() < Decimal("100")
    assert price.valid_from is not None
    assert price.valid_to is not None
    assert price.valid_to > price.valid_from
