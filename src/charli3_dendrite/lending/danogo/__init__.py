"""Read-only Danogo Float-Rate Lending analytics.

Importing this module registers the Danogo aggregator resolver. Danogo classes do not
subclass DEX bases and are not exported from `charli3_dendrite/__init__.py`.
"""

from charli3_dendrite.lending.danogo.loader import build_book
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.oracles.aggregator import DanogoAggregatorResolver
from charli3_dendrite.lending.danogo.state import DanogoLoanState
from charli3_dendrite.lending.danogo.state import DanogoPoolState
from charli3_dendrite.lending.oracles.models import register_resolver

register_resolver(DanogoAggregatorResolver())

__all__ = [
    "DanogoLoanState",
    "DanogoMarket",
    "DanogoPoolState",
    "build_book",
]
