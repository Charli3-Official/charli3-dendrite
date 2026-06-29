"""resolve_prices builds a PriceMap from the registry + live leaves; degrades safely."""
import json
from pathlib import Path
from types import SimpleNamespace

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.danogo.oracles.forward import resolve_prices
from charli3_dendrite.lending.danogo.oracles.locator import recipe_from_observation
from charli3_dendrite.lending.oracles.models import PriceMap

FWD = json.loads(
    (Path(__file__).parent / "fixtures" / "forward_pricing.json").read_text()
)


class _StubBackend:
    """Serves captured leaf UTxOs by address (and optional asset filter)."""

    def __init__(self, leaves):
        self._by_addr = {}
        for lf in leaves:
            root = {}
            for p, n, q in lf["assets"]:
                unit = "lovelace" if not p else f"{p}{n}"
                root[unit] = root.get(unit, 0) + int(q)
            self._by_addr.setdefault(lf["address"], []).append(
                SimpleNamespace(
                    address=lf["address"],
                    datum_cbor=lf["datum"],
                    assets=Assets(root=root),
                )
            )

    def get_pool_utxos(
        self, addresses, assets=None, limit=1000, page=0, historical=True
    ):
        out = []
        for addr in addresses:
            for info in self._by_addr.get(addr, []):
                if assets:
                    if not all(a in info.assets.root for a in assets):
                        continue
                out.append(info)
        return out


def test_resolve_prices_degrades_when_empty():
    pm = resolve_prices(_StubBackend([]), registry={}, pairs=[])
    assert isinstance(pm, PriceMap) and pm.prices == {}


def test_resolve_prices_reproduces_a_captured_price():
    case = FWD[0]
    registry = {
        f'{case["token"]}|{case["quote"]}': recipe_from_observation(case["leaves"])
    }
    backend = _StubBackend(case["leaves"])
    pm = resolve_prices(
        backend, registry=registry, pairs=[(case["token"], case["quote"])]
    )
    price = pm.get(case["token"])
    assert price is not None
    from fractions import Fraction

    assert Fraction(price.num, price.denom) == Fraction(
        case["expected"][0], case["expected"][1]
    )
    assert price.quote == case["quote"]
