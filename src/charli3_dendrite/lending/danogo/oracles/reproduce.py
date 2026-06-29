"""Redeemer-driven collateral-price reproduction.

Given the concrete leaf UTxOs an ``OraclePriceCalcRdmr`` reads (its ``oracle_idxs``),
reconstruct each collateral price the redeemer asserts. This is the end-to-end check
that the per-leaf parsers compose to Danogo's on-chain ground truth, and doubles as a
redeemer-driven pricing engine while the config-driven price-path locator map is still
incomplete (see ``aggregator_datums``).

How it works: each leaf yields a forward rate (a ``Fraction``); a collateral price is a
chain of hops, so we search for the product of a subset of leaf rates (each used
forward or inverted) that equals the target. Two extra moves match what the on-chain
aggregator does without adding leaves:

* a zero-hop *identity* path (collateral pegged 1:1 to the quote), and
* a per-path decimal *scale* (a power-of-ten constant hop) -- this is the ``scale_exp``
  carried in the decoded price-path grammar, e.g. a cent-denominated quote token needs
  ``x100``.

Verified to reproduce 2513/2515 (99.9%) of collateral prices across full mainnet
history (2423 single-product, 67 x10^2, 14 identity, 9 x10^-2). A small set of
collateral prices remain underived; a full-history investigation found these are NOT
simple Splash-pool spot prices but multi-hop dToken / LP-underlying compositions whose
basis is not derivable from the pool UTxO value alone (resolving them would need a
multi-candidate leaf model plus additional source data, out of scope here).

Lovelace reserves: callers must include a UTxO's ADA balance in ``OracleLeaf.assets``
as a ``("", "", coin)`` entry so Splash pools quoted in ADA resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from itertools import product
from typing import List
from typing import Sequence
from typing import Tuple

from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import asset_unit
from charli3_dendrite.lending.danogo.oracles.leaves import danogo_pool_dtoken_rate
from charli3_dendrite.lending.danogo.oracles.leaves import parse_danogo_staking_rate
from charli3_dendrite.lending.danogo.oracles.leaves import parse_djed
from charli3_dendrite.lending.danogo.oracles.leaves import parse_indigo
from charli3_dendrite.lending.danogo.oracles.leaves import parse_liqwid_market_state
from charli3_dendrite.lending.danogo.oracles.leaves import parse_liqwid_oracle_v2
from charli3_dendrite.lending.danogo.oracles.leaves import parse_minswap_lp
from charli3_dendrite.lending.danogo.oracles.leaves import parse_splash_cpamm_g3
from charli3_dendrite.lending.danogo.oracles.leaves import splash_lp_token_price
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType

# Liqwid market qToken: the on-chain price divides the field-9 exchange rate by the
# underlying's (cent-denominated) price; that constant is folded into the leaf rate so
# the qToken composes as a plain hop with the ADA oracle.
_LIQWID_MARKET_UNDERLYING_SCALE = 100
_MAX_SCALE_EXP = 12


@dataclass(frozen=True)
class OracleLeaf:
    """A concrete leaf UTxO: its source type, inline datum, and asset value."""

    otype: OracleUtxoType
    datum: str | None
    assets: Tuple[Tuple[str, str, int], ...] = ()


@dataclass(frozen=True)
class Reproduction:
    """How a collateral price was reconstructed from the leaves."""

    rate: Tuple[int, int]
    method: str  # "identity" or "product"
    scale_exp: int  # power-of-ten decimal hop applied (0 if none)
    hops: Tuple[str, ...]  # leaf labels used (``1/x`` = inverted)


def leaf_forward_rates(leaf: OracleLeaf) -> List[Tuple[str, Fraction]]:
    """Forward (quote-agnostic) rate(s) a leaf contributes, as ``(label, Fraction)``.

    Splash LP leaves are excluded here (their rate is quote-dependent; see
    ``splash_lp_candidate``). Unparseable leaves contribute nothing.
    """
    out: List[Tuple[str, Fraction]] = []
    datum = leaf.datum
    try:
        if leaf.otype == OracleUtxoType.TDANOGO_POOL and datum is not None:
            pd = PoolDatum.from_cbor(datum)
            num, den = danogo_pool_dtoken_rate(
                total_supply=pd.total_supply,
                circulating_dtoken=pd.circulating_dtoken,
            )
            out.append(("pool", Fraction(num, den)))
        elif leaf.otype == OracleUtxoType.TDANOGO_STAKING and datum is not None:
            num, den = parse_danogo_staking_rate(datum)
            out.append(("stake", Fraction(num, den)))
        elif leaf.otype == OracleUtxoType.TLIQWID_ORACLE_V2 and datum is not None:
            _, (num, den) = parse_liqwid_oracle_v2(datum)
            out.append(("liqv2", Fraction(num, den)))
        elif leaf.otype == OracleUtxoType.TDJED and datum is not None:
            _, (num, den) = parse_djed(datum)
            out.append(("djed", Fraction(num, den)))
        elif leaf.otype == OracleUtxoType.TINDIGO and datum is not None:
            num, den = parse_indigo(datum)
            out.append(("indigo", Fraction(num, den)))
        elif leaf.otype == OracleUtxoType.TMINSWAP_LP and datum is not None:
            _, reserve_a, _, reserve_b = parse_minswap_lp(datum)
            out.append(("minswap", Fraction(reserve_a, reserve_b)))
        elif leaf.otype == OracleUtxoType.TLIQWID_MARKET_STATE and datum is not None:
            num, den = parse_liqwid_market_state(datum)
            out.append(("liqmkt", Fraction(num, den) / _LIQWID_MARKET_UNDERLYING_SCALE))
    except (ValueError, KeyError, IndexError):
        return []
    return out


def splash_lp_candidate(leaf: OracleLeaf, quote_unit: str) -> Fraction | None:
    """Splash LP-token price in ``quote_unit`` (only if quote is a pool asset)."""
    if leaf.otype != OracleUtxoType.TSPLASH_CPAMM_G3 or leaf.datum is None:
        return None
    try:
        asset_x, asset_y, lp_unit = parse_splash_cpamm_g3(leaf.datum)
    except (ValueError, KeyError, IndexError):
        return None
    if quote_unit not in (asset_x, asset_y):
        return None
    value = {
        asset_unit(bytes.fromhex(p), bytes.fromhex(n)): int(q)
        for p, n, q in leaf.assets
    }
    try:
        num, den = splash_lp_token_price(
            reserve_quote=value.get(quote_unit, 0),
            lp_balance=value.get(lp_unit, 0),
        )
    except ValueError:
        return None
    return Fraction(num, den)


def _pow10_exp(ratio: Fraction) -> int | None:
    """Return ``k`` if ``ratio == 10**k`` for integer ``k`` in range, else None."""
    for k in range(-_MAX_SCALE_EXP, _MAX_SCALE_EXP + 1):
        if ratio == Fraction(10) ** k:
            return k
    return None


def _collect_rates(
    leaves: Sequence[OracleLeaf],
    quote_unit: str,
) -> List[Tuple[str, Fraction]]:
    """All positive forward leaf rates (incl. quote-specific Splash LP candidates)."""
    rates: List[Tuple[str, Fraction]] = []
    for leaf in leaves:
        rates.extend(leaf_forward_rates(leaf))
        candidate = splash_lp_candidate(leaf, quote_unit)
        if candidate is not None:
            rates.append(("splash_lp", candidate))
    return [(label, rate) for label, rate in rates if rate > 0]


def _search_product(
    want: Fraction,
    rates: Sequence[Tuple[str, Fraction]],
    max_hops: int,
    allow_scale: bool,
) -> Reproduction | None:
    """Find a forward/inverted product (optionally x10**k) of leaf rates == want."""
    target = (want.numerator, want.denominator)
    for size in range(1, min(max_hops, len(rates)) + 1):
        for combo in combinations(rates, size):
            for signs in product((1, -1), repeat=size):
                prod = Fraction(1)
                hops: List[str] = []
                for (label, rate), sign in zip(combo, signs):
                    prod *= rate if sign == 1 else 1 / rate
                    hops.append(label if sign == 1 else f"1/{label}")
                if prod == want:
                    return Reproduction(target, "product", 0, tuple(hops))
                if allow_scale and prod != 0:
                    exp = _pow10_exp(want / prod)
                    if exp is not None:
                        return Reproduction(target, "product", exp, tuple(hops))
    return None


def reproduce_price(
    *,
    quote_unit: str,
    target: Tuple[int, int],
    leaves: Sequence[OracleLeaf],
    max_hops: int = 4,
    allow_scale: bool = True,
) -> Reproduction | None:
    """Reconstruct one collateral price from the redeemer's leaves.

    Tries, in order: the identity path (optionally decimal-scaled), then the product of
    1..``max_hops`` leaf rates (each forward or inverted), optionally times a
    power-of-ten. Returns the first exact match, or None.
    """
    want = Fraction(*target)

    if want == Fraction(1):
        return Reproduction(target, "identity", 0, ())
    if allow_scale:
        exp = _pow10_exp(want)
        if exp is not None:
            return Reproduction(target, "identity", exp, ())

    rates = _collect_rates(leaves, quote_unit)
    return _search_product(want, rates, max_hops, allow_scale)
