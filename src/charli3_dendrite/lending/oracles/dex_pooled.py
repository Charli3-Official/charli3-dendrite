"""AMM-derived price from a dendrite-indexable DEX pool UTxO."""

from __future__ import annotations

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource


class DexPooledResolver:
    """price(token in quote) = quote_reserve / token_reserve."""

    source = OracleSource.DEX_POOLED

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """UTxO selectors needed to resolve these DEX-pool refs."""
        selectors = []
        for ref in refs:
            sel = ref.selector()
            if sel is not None:
                selectors.append(sel)
        return selectors

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Derive a spot price from the matching pool UTxO's reserves."""
        token_unit = ref.extra.get("reserve_token", ref.token)
        quote_unit = ref.extra.get("reserve_quote", ref.quote)
        for info in utxos:
            if ref.address is not None and info.address != ref.address:
                continue
            reserves = info.assets.root
            if token_unit not in reserves or quote_unit not in reserves:
                continue
            token_reserve = reserves[token_unit]
            quote_reserve = reserves[quote_unit]
            if token_reserve <= 0 or quote_reserve <= 0:
                continue
            # Coarse fallback oracle: raw reserve ratio ignores pool fees and
            # token decimals; the first matching pool UTxO wins.
            return OraclePrice(
                token=ref.token,
                quote=ref.quote,
                num=quote_reserve,
                denom=token_reserve,
                source=self.source,
                valid_from=info.block_time * 1000,
                valid_to=None,
            )
        return None
