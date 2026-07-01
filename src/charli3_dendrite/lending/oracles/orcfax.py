"""Orcfax fact-statement (CER) datum + resolver.

Layout follows the Orcfax fact-statement (CER) encoding, exercised by the fixture
`tests/lending/fixtures/orcfax_feed.json`. Mainnet currently publishes the compact
aiken `statement` form, not the verbose JSON-LD `PropertyValue` form::

    FeedDatum = Constr 0 [ Statement, Context ]
    Statement = Constr 0 [ feed_id: bytes, created_at_ms: int, OrcfaxRational ]
    OrcfaxRational = Constr 0 [ num: int, denom: int ]
    Context   = Constr 0 [ identifier: bytes ]   # opaque, read as RawPlutusData

The price is the exact rational `num / denom` (e.g. 163333 / 1000000 ≈ 0.1633
USD per ADA). The compact statement carries no explicit validity window, so
`valid_to` falls back to `created_at + ORCFAX_TOLERANCE_MS`.

Each Orcfax fact statement is published to the FS validator address carrying a
single FS token (policy == FS validator script hash, empty token name), so the
feed-NFT unit alone does not disambiguate ADA-USD from sibling feeds (ADA-USDA,
ADA-USDM, ...). The resolver therefore filters on the datum's `feed_id`.
"""

from __future__ import annotations

from dataclasses import dataclass

from pycardano import DeserializeException
from pycardano import PlutusData
from pycardano import RawPlutusData

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource

ORCFAX_TOLERANCE_MS = 6 * 60 * 60 * 1000  # 6h fallback validity tolerance


@dataclass
class OrcfaxRational(PlutusData):
    """Price as the exact rational num / denom."""

    CONSTR_ID = 0
    num: int
    denom: int


@dataclass
class OrcfaxStatement(PlutusData):
    """Signed Orcfax fact statement: feed id, creation time and price body."""

    CONSTR_ID = 0
    feed_id: bytes
    created_at: int
    body: OrcfaxRational


@dataclass
class OrcfaxFeedDatum(PlutusData):
    """Orcfax feed UTxO datum; `context` is left opaque (collector identity)."""

    CONSTR_ID = 0
    statement: OrcfaxStatement
    context: RawPlutusData


def _has_unit(info: PoolStateInfo, unit: str) -> bool:
    """Return True if `unit` (policy+name) is present on the UTxO."""
    return unit in info.assets.root


def _feed_id_matches(feed_id: bytes, prefix_hex: str | None) -> bool:
    """Match the statement `feed_id` against an expected prefix, anchored at `/`.

    Orcfax may publish a versioned feed id (e.g. `CER/ADA-USD/3`), so accept an
    exact match or a prefix that ends on a `/` boundary. This never accepts a
    partial token (so `CER/ADA-USD/3` rejects `CER/ADA-USD/30`, and the bare
    `CER/ADA-USD` rejects sibling `CER/ADA-USDA/...`).
    """
    if not prefix_hex:
        return True
    try:
        prefix = bytes.fromhex(prefix_hex)
    except ValueError:
        prefix = prefix_hex.encode()
    return feed_id == prefix or feed_id.startswith(prefix + b"/")


class OrcfaxResolver:
    """Resolves an Orcfax CER fact-statement UTxO to an OraclePrice."""

    source = OracleSource.ORCFAX

    def selectors(self, refs: list[OracleRef]) -> list[PoolSelector]:
        """UTxO selectors needed to resolve these refs."""
        selectors = []
        for ref in refs:
            sel = ref.selector()
            if sel is not None:
                selectors.append(sel)
        return selectors

    def resolve(self, ref: OracleRef, utxos: PoolStateList) -> OraclePrice | None:
        """Find + parse the freshest Orcfax feed for `ref` among `utxos`.

        Matches on the feed-NFT unit (when supplied) and, crucially, on the
        datum `feed_id` so sibling feeds sharing the policy are not confused.

        Orcfax publishes a new ADA-USD fact statement roughly hourly, and stale
        fact-statement UTxOs may remain UNSPENT at the FS address. A single
        address+asset query can therefore return many matching statements of
        varying ages. Returning the first match (UTxO order is undefined) risks
        handing a liquidation bot a STALE price. So among every UTxO that passes
        the feed-NFT + feed_id + positive-rational guards, select the statement
        with the greatest `created_at` (the freshest published fact).
        """
        unit = (ref.feed_policy or "") + (ref.feed_name or "")
        best: OraclePrice | None = None
        best_created_at = -1
        for info in utxos:
            if unit and not _has_unit(info, unit):
                continue
            if info.datum_cbor is None:
                continue
            # Every FS statement shares the same FS token, so foreign/corrupt
            # datums reach this point; skip them instead of aborting the batch.
            # pycardano signals a shape mismatch with DeserializeException or a
            # constructor TypeError/ValueError/IndexError depending on the input.
            try:
                datum = OrcfaxFeedDatum.from_cbor(info.datum_cbor)
            except (DeserializeException, TypeError, ValueError, IndexError):
                continue
            stmt = datum.statement
            if not _feed_id_matches(stmt.feed_id, ref.feed_id):
                continue
            # On-chain values: refuse a non-positive rational that would later
            # explode (zero-division) or invert in `OraclePrice.as_decimal()`.
            if stmt.body.denom <= 0 or stmt.body.num <= 0:
                continue
            if stmt.created_at <= best_created_at:
                continue
            best_created_at = stmt.created_at
            best = OraclePrice(
                token=ref.token,
                quote=ref.quote,
                num=stmt.body.num,
                denom=stmt.body.denom,
                source=self.source,
                valid_from=stmt.created_at,
                valid_to=stmt.created_at + ORCFAX_TOLERANCE_MS,
            )
        return best
