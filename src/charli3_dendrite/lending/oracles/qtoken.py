"""Receipt-token (qToken/dToken) exchange-rate resolver.

The rate is provided per-call via OracleRef.extra (`num`/`denom`), sourced from
the pool/market datum by the concrete protocol class.
"""

from __future__ import annotations

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource


class QTokenResolver:
    """Normalizes a supplied qToken exchange rate to an OraclePrice."""

    source = OracleSource.QTOKEN_RATE

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """UTxO selectors (none: rate is supplied in-band by the pool state)."""
        return []

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Build an OraclePrice from the rate supplied in `ref.extra`."""
        num = ref.extra.get("num")
        denom = ref.extra.get("denom")
        if num is None or denom is None:
            return None
        # A non-numeric `extra` value should yield no price, not an exception,
        # mirroring the sibling resolvers' defensive parsing.
        try:
            num = int(num)
            denom = int(denom)
        except (TypeError, ValueError):
            return None
        # Refuse a non-positive rate that would later zero-divide or invert in
        # `OraclePrice.as_decimal()`, mirroring the sibling resolvers' guard.
        if denom <= 0 or num <= 0:
            return None
        return OraclePrice(
            token=ref.token,
            quote=ref.quote,
            num=num,
            denom=denom,
            source=self.source,
            valid_from=ref.extra.get("valid_from"),
            valid_to=ref.extra.get("valid_to"),
        )
