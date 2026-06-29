"""Read-only lending-protocol analytics (pools, loans, oracles).

Importing this package registers all oracle resolvers. Lending classes are
intentionally NOT exported from `charli3_dendrite/__init__.py` and do not
subclass DEX bases, so they never enter DEX discovery.
"""

from charli3_dendrite.lending.base import AbstractLendingPoolState
from charli3_dendrite.lending.base import AbstractLoanState
from charli3_dendrite.lending.base import LendingBook
from charli3_dendrite.lending.base import LendingPriceBook
from charli3_dendrite.lending.oracles.charli3 import Charli3Resolver
from charli3_dendrite.lending.oracles.dex_pooled import DexPooledResolver
from charli3_dendrite.lending.oracles.fluid import FluidResolver
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.oracles.models import register_resolver
from charli3_dendrite.lending.oracles.orcfax import OrcfaxResolver
from charli3_dendrite.lending.oracles.qtoken import QTokenResolver

register_resolver(Charli3Resolver())
register_resolver(OrcfaxResolver())
register_resolver(QTokenResolver())
register_resolver(DexPooledResolver())
register_resolver(FluidResolver(OracleSource.FLUID_AGGREGATED))
register_resolver(FluidResolver(OracleSource.FLUID_POOLED))
register_resolver(FluidResolver(OracleSource.FLUID_DEDICATED))

__all__ = [
    "AbstractLendingPoolState",
    "AbstractLoanState",
    "LendingBook",
    "LendingPriceBook",
    "OraclePrice",
    "OracleRef",
    "OracleSource",
    "PriceMap",
]
