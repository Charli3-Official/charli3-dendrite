"""Charli3 aggregated-oracle datum + resolver.

Layout (exercised by the test fixture):
    OracleDatum  = Constr 0 [ GenericData ]
    GenericData  = Constr 2 [ Map{0: price, 1: valid_from_ms, 2: valid_to_ms} ]
`price` is scaled by 10**decimals (decimals supplied per-feed via OracleRef).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from pycardano import PlutusData

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource

PRICE_KEY = 0
VALID_FROM_KEY = 1
VALID_TO_KEY = 2


@dataclass
class Charli3GenericData(PlutusData):
    """Charli3 GenericData: integer-keyed price map."""

    CONSTR_ID = 2
    price_map: Dict[int, int]


@dataclass
class Charli3OracleDatum(PlutusData):
    """Charli3 oracle UTxO datum."""

    CONSTR_ID = 0
    price_data: Charli3GenericData


def _has_unit(info: PoolStateInfo, unit: str) -> bool:
    return unit in info.assets.root


class Charli3Resolver:
    """Resolves a Charli3 feed UTxO to an OraclePrice."""

    source = OracleSource.CHARLI3

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """UTxO selectors needed to resolve these refs."""
        selectors = []
        for ref in refs:
            sel = ref.selector()
            if sel is not None:
                selectors.append(sel)
        return selectors

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Find + parse the Charli3 feed for `ref` among `utxos`."""
        unit = (ref.feed_policy or "") + (ref.feed_name or "")
        for info in utxos:
            if unit and not _has_unit(info, unit):
                continue
            if info.datum_cbor is None:
                continue
            datum = Charli3OracleDatum.from_cbor(info.datum_cbor)
            price_map = datum.price_data.price_map
            if PRICE_KEY not in price_map:
                continue
            return OraclePrice(
                token=ref.token,
                quote=ref.quote,
                num=price_map[PRICE_KEY],
                denom=10**ref.decimals,
                source=self.source,
                valid_from=price_map.get(VALID_FROM_KEY),
                valid_to=price_map.get(VALID_TO_KEY),
            )
        return None
