"""Cross-check oracle feed resolution for live cross-quote builds.

Recipe-driven leaf resolution (`_resolve_oracle_source_leaves`) only fetches the
source leaves the mined pricing recipes name. A cross-quote market's ``ada -> quote``
conversion is additionally averaged across, and deviation-checked against, independent
external price feeds (e.g. an Indigo ``ada -> quote`` feed) that no recipe references,
so the oracle ``Withdraw`` walks those cross-check feeds. These tests cover the
supplemental fetch that makes a fully-live cross-quote ``from_backend`` snapshot carry
the curated cross-check feed so the deviation filter can reference it.

The gate is exercised offline (an unmapped / ADA quote pulls nothing without touching
the backend); the live fetch is dbsync-gated and proves the curated USDCx Indigo handle
resolves and lands in the live snapshot's source leaves. Whether the deviation filter
then *references* the leaf depends on the live feed's freshness; the byte-exact
selection (when the feed is within tolerance) is covered by the captured collateral-
remove repay fixture and its Ogmios evaluation.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

from charli3_dendrite.lending.danogo.constants import (  # noqa: E402
    CROSS_CHECK_SUPPLEMENTAL_HANDLES,
)
from charli3_dendrite.lending.danogo.constants import _addr  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    RepaySnapshot,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    _resolve_cross_check_supplemental_leaves,
)

# USDCx-supply cross-quote market: supply token, the captured collateral-remove
# market's pool-NFT name, and the loan-token policy (loan_skh) loan UTxOs carry.
USDCX_SUPPLY = "1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378"
REMOVE_MARKET = "6ac29b9eabd162cd37479f1c7fa61410364c78ac4601aebd2c079e4c"
LOAN_SKH = "aca8e306eda3eb6c25a838bebac37d929c216aab13c8d463fca5a08d"

_DBSYNC = pytest.mark.skipif(
    not os.environ.get("DBSYNC_HOST"),
    reason="requires live dbsync (DBSYNC_HOST/USER/PASS/PORT/DB_NAME)",
)


def _backend():  # noqa: ANN202
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _open_loan_out_ref(backend) -> str:  # noqa: ANN001
    """The ``tx_hash#index`` of any currently-open loan in the USDCx market."""
    unit = LOAN_SKH + REMOVE_MARKET
    for info in backend.get_pool_utxos(addresses=[_addr(LOAN_SKH)], historical=False):
        if info.assets.root.get(unit) == 1:
            return f"{info.tx_hash}#{info.tx_index}"
    pytest.skip("no open loan found for the USDCx market")


def test_unmapped_quote_fetches_nothing() -> None:
    """A quote with no curated cross-check entry pulls nothing (no backend call).

    The gate returns before any backend use, so a never-called stand-in is enough.
    ADA-quote / single-hop markets (and any unmapped supply token) are untouched.
    """
    assert USDCX_SUPPLY in CROSS_CHECK_SUPPLEMENTAL_HANDLES
    sentinel = object()
    assert _resolve_cross_check_supplemental_leaves(sentinel, quote="lovelace") == []  # type: ignore[arg-type]
    assert _resolve_cross_check_supplemental_leaves(sentinel, quote="deadbeef") == []  # type: ignore[arg-type]


@_DBSYNC
def test_supplemental_handle_resolves_live() -> None:
    """The curated USDCx Indigo cross-check handle resolves to one live UTxO."""
    handle = CROSS_CHECK_SUPPLEMENTAL_HANDLES[USDCX_SUPPLY][0]
    leaves = _resolve_cross_check_supplemental_leaves(_backend(), quote=USDCX_SUPPLY)
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.holds(handle.nft_policy, handle.nft_name, 1)
    assert leaf.out_ref is not None


@_DBSYNC
def test_from_backend_includes_cross_check_leaf() -> None:
    """A live cross-quote ``from_backend`` snapshot carries the Indigo cross-check leaf.

    Recipe-driven resolution alone returns only the recipe leaves -- no recipe names an
    Indigo source -- so without this fetch a fully-live build could never reference the
    path config's cross-check feed. The supplemental fetch adds it to
    ``oracle_source_leaves`` additively (the deviation filter still decides whether it
    is referenced), and the leaf is exactly the one the supplemental resolver pins.
    """
    backend = _backend()
    snapshot = RepaySnapshot.from_backend(
        backend,
        market_name=REMOVE_MARKET,
        loan_utxo=_open_loan_out_ref(backend),
    )
    handle = CROSS_CHECK_SUPPLEMENTAL_HANDLES[USDCX_SUPPLY][0]
    indigo = [
        u
        for u in snapshot.oracle_source_leaves
        if u.holds(handle.nft_policy, handle.nft_name, 1)
    ]
    assert len(indigo) == 1
    supplemental = _resolve_cross_check_supplemental_leaves(backend, quote=USDCX_SUPPLY)
    assert indigo[0].out_ref == supplemental[0].out_ref
