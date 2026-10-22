"""FluidTokens V4 views frozen in a golden file.

Every FluidTokens V4 fixture pool and loan, and the fixture borrow request, is
converted to its view and serialised together with the loans' debt, interest and
health at a fixed time and fixed prices. The test fails when a single serialised
byte changes, which is what an indexer storing ``view.to_dict()`` would see.

Regenerate the golden file only for an intended change to the views::

    python tests/lending/test_views_golden.py --write
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from charli3_dendrite.lending.fluidtokens_v4.indexing import parse_utxo
from charli3_dendrite.lending.fluidtokens_v4.market import FluidTokensV4MarketSource
from charli3_dendrite.lending.fluidtokens_v4.market import request_to_borrow_request
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4RequestState
from charli3_dendrite.lending.normalized import BorrowRequest
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from tests.lending.fluidtokens_v4.records import FIX
from tests.lending.fluidtokens_v4.records import record_info
from tests.lending.fluidtokens_v4.records import v4_request_record

GOLDEN_PATH: Path = Path(__file__).parent / "fixtures" / "ft_v4_views_golden.json"
NOW_MS: int = 1_790_000_000_000

# Every non-lovelace unit the fixture pools, loans and request name (borrow units and
# collateral units), in sorted order; each is priced in lovelace from its index.
_PRICED_UNITS: tuple[str, ...] = (
    "0691b2fecca1ac4f53cb6dfb00b7013e561d1f34403b957cbb5af1fa4e49474854",
    "1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378",
    "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f534e454b",
    "577f0b1342f8f8f4aed3388b80a8535812950c7a892495c0ecdf0f1e0014df10464c4454",
    "5d16cc1a177b5d9ba9cfa9793b07e60f1fb70fea1f8aef064415d114494147",
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61446a65644d6963726f555344",
    "9ff9a1b456f074e03be90631e1a5f9b6ed08eacabd0e7f95a11ffff10014df1041544c4153",
    "a0028f350aaabe0545fdcb56b039bfb08e4bb4d8c4d7c3c7d481c235484f534b59",
    "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d",
    "e5a42a1a1d3d1da71b0449663c32798725888d2eb0843c4dabeca05a576f726c644d6f62696c65546f6b656e58",
    "eb7a93ebc321647673490810f618b548d7c24aa64d30ae342dba70760014df10415343454e44",
    "f13ac4d66b3ee19a6aa0f2a22298737bd907cc95121662fc971b5275535452494b45",
    "fe7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae45655534441",
)


def golden_prices() -> PriceMap:
    """Fixed lovelace prices for every unit the fixture views name.

    The unit at sorted index ``i`` costs ``(2 * i + 3) / (i + 5)`` lovelace, so every
    unit has a distinct positive price.
    """
    prices = PriceMap()
    for index, unit in enumerate(_PRICED_UNITS):
        prices.add(
            OraclePrice(
                token=unit,
                quote="lovelace",
                num=2 * index + 3,
                denom=index + 5,
                source=OracleSource.CHARLI3,
            ),
        )
    return prices


def _pools() -> list[LendingMarket]:
    source = FluidTokensV4MarketSource()
    pools: list[LendingMarket] = []
    for rec in FIX["pool"]:
        view = source.parse(record_info(rec))
        if not isinstance(view, LendingMarket):
            raise TypeError(f"pool {rec['out_ref']} did not parse as a LendingMarket")
        pools.append(view)
    return pools


def _loans() -> list[LendingPosition]:
    source = FluidTokensV4MarketSource()
    loans: list[LendingPosition] = []
    for rec in FIX["loan"]:
        view = source.parse(record_info(rec))
        if not isinstance(view, LendingPosition):
            raise TypeError(f"loan {rec['out_ref']} did not parse as a LendingPosition")
        loans.append(view)
    return loans


def _request() -> BorrowRequest:
    state = parse_utxo(record_info(v4_request_record()))
    if not isinstance(state, FluidV4RequestState):
        raise TypeError("the fixture request did not parse as a request")
    return request_to_borrow_request(state)


def build_golden() -> dict[str, Any]:
    """Every fixture view, serialised, with debt, interest and health at NOW_MS."""
    prices = golden_prices()
    request = _request()
    principal_range = request.principal_range(prices)
    return {
        "pools": [pool.to_dict() for pool in _pools()],
        "loans": [
            {
                "view": loan.to_dict(),
                "current_debt": loan.current_debt(NOW_MS),
                "interest_accrued": loan.interest_accrued(NOW_MS),
                "health_factor": str(loan.health_factor(prices, NOW_MS)),
            }
            for loan in _loans()
        ],
        "request": {
            "view": request.to_dict(),
            "principal_range": (
                None if principal_range is None else list(principal_range)
            ),
        },
    }


def _canonical(data: Any) -> str:  # noqa: ANN401
    return json.dumps(data, sort_keys=True)


def test_ft_v4_views_match_golden() -> None:
    """Every FluidTokens V4 view serialises byte-for-byte as the golden file."""
    golden = json.loads(GOLDEN_PATH.read_text())
    assert _canonical(build_golden()) == _canonical(golden)


def test_golden_prices_cover_every_view_unit() -> None:
    """Every fixture view is complete and every unit it names has a golden price."""
    pools = _pools()
    loans = _loans()
    request = _request()
    assert len(pools) == len(FIX["pool"])
    assert len(loans) == len(FIX["loan"])
    named = {request.borrow_unit, request.collateral.unit, request.principal_limit.unit}
    for pool in pools:
        named.add(pool.borrow_unit)
        named.update(terms.unit for terms in pool.collateral)
    for loan in loans:
        named.add(loan.borrow_unit)
        named.update(holding.unit for holding in loan.collateral)
    named.discard("lovelace")
    prices = golden_prices()
    assert sorted(named) == list(_PRICED_UNITS)
    assert all(prices.get(unit) is not None for unit in named)


def write_golden() -> None:
    """Regenerate the golden file from the current views."""
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(build_golden(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        sys.exit("usage: python tests/lending/test_views_golden.py --write")
    write_golden()
