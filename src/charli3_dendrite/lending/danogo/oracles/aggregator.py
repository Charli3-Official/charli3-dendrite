"""Danogo oracle-aggregator price-derivation engine + resolver.

The pure engine functions (`calc_out_amount`, `derive_path_price`,
`average_paths`) are deterministic, exact-integer, and decoupled from chain
data: they operate on plain numbers / `ref.extra`. A future protocol with
multi-hop oracle routing is likely to want them, so they stay cleanly
separated from anything Danogo-specific.

NOTE: wiring real leaf UTxOs -> per-hop (num, denom, is_reverse) rationals
lives in the loader. The engine here stays pure and operates only on
`ref.extra['paths']`.
"""

from __future__ import annotations

from pydantic import ValidationError

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource

_COMMON = 10**18  # common denominator for averaging in exact integers


def calc_out_amount(*, in_amount: int, num: int, denom: int, is_reverse: bool) -> int:
    """On-chain calc_out_amount with the [M302] divide-by-zero fix.

    This is the standalone on-chain per-hop reference replica, kept for
    cross-checks against the validator's per-hop semantics. It is NOT
    called by `derive_path_price` (which composes the equivalent rational per
    hop inline); the two are kept in sync by hand so the single-hop semantics
    here document exactly what each hop in a chained path should compute.
    """
    if is_reverse:
        if num <= 0:
            raise ValueError("reverse hop numerator <= 0")
        return in_amount * denom // num
    if denom <= 0:
        raise ValueError("forward hop denominator <= 0")
    return in_amount * num // denom


def derive_path_price(hops: list[tuple[int, int, bool]]) -> tuple[int, int]:
    """Chain hops (num, denom, is_reverse) into one (num, denom) price.

    Multiplies rational factors; each hop respects the [M302] orientation guard
    (the per-hop semantics mirror `calc_out_amount`). An empty hop list is
    rejected so a path with no hops cannot inject a spurious price=1.
    """
    if not hops:
        raise ValueError("empty hop list")
    num, denom = 1, 1
    for hop_num, hop_denom, is_reverse in hops:
        if is_reverse:
            if hop_num <= 0:
                raise ValueError("reverse hop numerator <= 0")
            num, denom = num * hop_denom, denom * hop_num
        else:
            if hop_denom <= 0:
                raise ValueError("forward hop denominator <= 0")
            num, denom = num * hop_num, denom * hop_denom
    return (num, denom)


def _median(values: list[int]) -> int:
    """Exact-integer median of a non-empty list."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def average_paths(
    path_prices: list[tuple[int, int]],
    *,
    deviation_bps: int,
) -> tuple[int, int]:
    """Average path prices, dropping any beyond deviation_bps of the median.

    Outlier rejection is anchored on the median rather than the mean: a single
    large outlier shifts the mean enough to drag the honest paths outside the
    tolerance band (and would otherwise trip the fallback that resurrects every
    path). The median is robust to that, so survivors are the paths genuinely
    close to the cluster; those survivors are then mean-averaged.

    Meaningful outlier rejection requires >=3 paths. With 1 path the result is
    that path's scaled value; with 2 paths the median is their midpoint and both
    are equidistant from it, so a real disagreement pushes BOTH outside the
    tolerance and the fallback averages them anyway. So for 1-2 paths the result
    is the plain (scaled) average with no filtering. This is intentional.

    Returns (num, _COMMON). Raises ValueError only on empty input or when every
    path has denom <= 0; once at least one path is valid the `or scaled`
    fallback guarantees survivors, so it never raises for "all filtered out".

    NOTE: the median-anchored rejection + re-average is a CHOSEN approximation of
    the on-chain aggregator's averaging/deviation-filtering, which is UNCONFIRMED.
    The target is bit-for-bit agreement with the validator, so this must be
    reconciled against the on-chain aggregator (mean vs median anchor, tolerance
    semantics, <3-path behavior) and adjusted if it diverges. This feeds
    liquidation decisions.
    """
    if not path_prices:
        raise ValueError("no paths")
    scaled = [num * _COMMON // denom for num, denom in path_prices if denom > 0]
    if not scaled:
        raise ValueError("no valid paths")
    center = _median(scaled)
    tol = center * deviation_bps // 10_000
    kept = [s for s in scaled if abs(s - center) <= tol] or scaled
    return (sum(kept) // len(kept), _COMMON)


class DanogoAggregatorResolver:
    """Resolves a collateral price via the Danogo aggregator (a PriceResolver)."""

    source = OracleSource.DANOGO_AGGREGATOR

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """Selectors for global-config + path + leaf UTxOs.

        Built from each ref's `extra` (populated by the loader), which carries the
        path/global-config/leaf locators. Returns [] when unpopulated.
        """
        selectors: list[PoolSelector] = []
        for ref in refs:
            for sel in ref.extra.get("selectors", []):
                if not isinstance(sel, dict):
                    continue
                try:
                    selectors.append(PoolSelector(**sel))
                except (TypeError, ValueError, ValidationError):
                    continue
        return selectors

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Derive `ref.token`->`ref.quote` price from the aggregator path config.

        The loader pre-attaches the resolved per-hop rationals on
        `ref.extra['paths']` as a list of paths, each a list of
        (num, denom, is_reverse). This keeps leaf parsing decoupled from the
        deterministic engine here.
        """
        paths = ref.extra.get("paths")
        if not paths:
            return None
        # A malformed deviation (None/str/list) falls back to 0 (no filtering)
        # rather than aborting the whole resolve, matching the sibling resolvers'
        # "skip, don't raise" defensive parsing.
        try:
            deviation = int(ref.extra.get("deviation_bps", 0))
        except (TypeError, ValueError):
            deviation = 0
        derived: list[tuple[int, int]] = []
        for hops in paths:
            # One malformed hop/path is skipped, not fatal: a short tuple raises
            # IndexError, a non-numeric element raises TypeError/ValueError, and
            # an empty path raises ValueError from derive_path_price.
            try:
                derived.append(
                    derive_path_price(
                        [(int(h[0]), int(h[1]), bool(h[2])) for h in hops],
                    ),
                )
            except (ValueError, TypeError, IndexError):
                continue
        if not derived:
            return None
        num, denom = average_paths(derived, deviation_bps=deviation)
        if num <= 0 or denom <= 0:
            return None
        return OraclePrice(
            token=ref.token,
            quote=ref.quote,
            num=num,
            denom=denom,
            source=self.source,
        )
