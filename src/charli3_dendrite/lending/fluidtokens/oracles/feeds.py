"""Best-effort locator for FluidTokens collateral price feeds.

Locates the Aggregated / Charli3 / Orcfax feed UTxOs that price loan collateral
into the principal asset and folds them into a `PriceMap`. Live-feed wiring is not
yet attached, so `resolve_prices` currently returns an empty `PriceMap`: the
snapshot loader degrades to it gracefully (an unpriced collateral is reported as
not-liquidatable, never a crash). It never fabricates a price.

Collateral oracle precision is intentionally out of scope here; perpetual
debt/health is driven by the loan datum and does not depend on this map.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Iterable

from charli3_dendrite.lending.oracles.models import PriceMap

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend


def resolve_prices(
    backend: AbstractBackend,  # noqa: ARG001 - design seam for live-feed wiring
    *,
    pairs: Iterable[tuple[str, str]] = (),  # noqa: ARG001 - (collateral, principal)
) -> PriceMap:
    """Resolve collateral prices for `(collateral, principal)` pairs.

    Best-effort stub pending live-feed wiring: returns an empty `PriceMap` so the
    snapshot degrades gracefully. `backend` and `pairs` are the seam a future
    revision uses to locate the Aggregated/Charli3/Orcfax feed UTxOs.
    """
    return PriceMap()
