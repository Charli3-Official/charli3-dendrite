"""Cross-quote collateral must be priced through an intermediate quote (ADA).

The partial-repay fixture locks a Danogo pool dtoken whose pool prices it in ADA,
while the market's supply token (the quote) is a different stablecoin. The on-chain
oracle ``Withdraw`` redeemer therefore declares TWO entries under that quote -- the
intermediate ``ada -> quote`` price AND the composed ``dtoken -> quote`` price
(``dtoken -> ada`` times ``ada -> quote``) -- and references both legs' source
leaves. The forward pricing engine must reproduce that map and leaf set byte-exact;
a single standalone ``dtoken -> quote`` price (the divergent recipe route, missing
the intermediate) is what this guards against.
"""

import json
from pathlib import Path
from typing import Callable

from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.transactions._common import _select_global_config
from charli3_dendrite.lending.danogo.transactions._common import _select_path_config
from charli3_dendrite.lending.danogo.transactions.build import (
    _forward_prices_and_leaves,
)
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.danogo.transactions.context import _loan_collateral_units

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PARTIAL = "decrease_loan_partial_tx.json"
REMOVE_COLLAT = "decrease_loan_remove_collat_tx.json"
# The structured (newer) deployment's collateral-ADD repay: priced via the on-chain
# path-config walk, not a mined recipe.
ADD_COLLAT = "decrease_loan_add_collat_tx.json"


def _captured_ref_index(fix: dict) -> dict[tuple[str, int], int]:
    """Map each reference input out-ref to its canonical Plutus script-context index."""
    refs = [(u["out_ref"][0], int(u["out_ref"][1])) for u in fix["ref_inputs"]]
    refs.sort(key=lambda r: (bytes.fromhex(r[0]), r[1]))
    return {r: pos for pos, r in enumerate(refs)}


def test_partial_repay_prices_collateral_through_intermediate(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    fix, snapshot = repay_snap(PARTIAL)
    captured = OraclePriceCalcRdmr.from_cbor(fix["oracle_redeemer"])

    collateral_units = _loan_collateral_units(
        snapshot.loan,
        loan_skh=snapshot.loan_skh,
        market_name=snapshot.market_name,
    )
    prices, used_leaves = _forward_prices_and_leaves(
        snapshot,
        set(collateral_units),
    )

    # The prices map reproduces both the intermediate (ada) and the composed
    # collateral entry exactly -- same quote, same units, same rationals.
    assert prices == captured.prices
    # ...and in the same map order (intermediate first), which the redeemer encodes
    # positionally, so a byte-exact rebuild needs the ordering preserved.
    quote = snapshot.market_info.supply_token
    assert list(prices[quote]) == list(captured.prices[quote])

    # The referenced leaves reproduce the captured oracle_idxs byte-exact: every
    # priced leaf maps to its canonical reference-input index, and the (target,
    # otype, idx) triples (sorted by idx, as the redeemer emits them) match.
    ref_index = _captured_ref_index(fix)
    got = sorted(
        (UTxOTarget.REF, otype, ref_index[leaf.out_ref])
        for leaf, otype in used_leaves
        if leaf.out_ref is not None
    )
    assert got == sorted(captured.oracle_idxs)


def test_structured_add_collat_repay_reproduces_oracle_redeemer(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    # The structured (newer) oracle deployment stores no mined recipe: the dToken
    # collateral is priced by walking the on-chain structured path config -- source 24
    # (Danogo pool) forward then source 18 (Indigo) reversed -- with the ADA
    # intermediate (source 18 reversed) declared first. Forward pricing must reproduce
    # the captured oracle redeemer byte-for-byte: the prices map (intermediate first,
    # exact rationals), the referenced source leaves IN PATH-WALK ORDER (pool then
    # Indigo, not sorted by index), and the global / path config indices.
    fix, snapshot = repay_snap(ADD_COLLAT)
    captured = OraclePriceCalcRdmr.from_cbor(fix["oracle_redeemer"])

    collateral_units = _loan_collateral_units(
        snapshot.loan,
        loan_skh=snapshot.loan_skh,
        market_name=snapshot.market_name,
    )
    prices, used_leaves = _forward_prices_and_leaves(snapshot, set(collateral_units))

    # The prices map (and its order: ADA intermediate first, then the dToken) matches.
    assert prices == captured.prices
    quote = snapshot.market_info.supply_token
    assert list(prices[quote]) == list(captured.prices[quote])

    # The structured deployment references its priced leaves in path-walk order, which
    # the redeemer encodes positionally -- so the leaf order must match exactly (NOT
    # only as a set): pool (idx 2) then Indigo (idx 1).
    ref_index = _captured_ref_index(fix)
    got = [
        (UTxOTarget.REF, otype, ref_index[leaf.out_ref])
        for leaf, otype in used_leaves
        if leaf.out_ref is not None
    ]
    assert got == captured.oracle_idxs

    # Reassemble the full redeemer (config indices included) and confirm it round-trips
    # to the captured oracle redeemer CBOR byte-for-byte.
    rebuilt = OraclePriceCalcRdmr(
        oracle_source_idx=ref_index[_select_global_config(snapshot).out_ref],
        oracle_path_idxs=[
            ref_index[
                _select_path_config(
                    snapshot,
                    supply_token=quote,
                    used_leaves=used_leaves,
                ).out_ref
            ],
        ],
        oracle_idxs=got,
        prices=prices,
        borrow_rates={},
    )
    assert rebuilt.to_cbor().hex() == fix["oracle_redeemer"]


def test_remove_collat_repay_reproduces_intermediate_price(
    repay_snap: Callable[[str], tuple[dict, RepaySnapshot]],
) -> None:
    # The collateral-removing repay rides a non-ADA supply-token (cross-quote) market
    # whose collateral recipe is intermediate-leading: it opens with the canonical
    # ada->quote leaf, then the collateral's pool leg. Forward pricing must still
    # declare the intermediate (ada) entry alongside the composed collateral price, so
    # the prices map reproduces the captured oracle redeemer's exactly -- same quote,
    # units, and rationals (intermediate included).
    #
    # The on-chain oracle Withdraw additionally references the routing path config's
    # alternative ada->quote source (an Indigo feed) that it deviation-checks the
    # composed intermediate rate against, even though that source does not change the
    # declared price. The forward-built leaf set must therefore match the captured
    # oracle_idxs byte-exact -- the recipe legs PLUS that cross-check source leaf.
    from charli3_dendrite.lending.danogo.transactions.repay import (
        _pool_holds_alt_supply,
    )

    fix, snapshot = repay_snap(REMOVE_COLLAT)
    captured = OraclePriceCalcRdmr.from_cbor(fix["oracle_redeemer"])

    units = set(
        _loan_collateral_units(
            snapshot.loan,
            loan_skh=snapshot.loan_skh,
            market_name=snapshot.market_name,
        ),
    )
    # The validator also re-prices the pool's held alternative supply tokens, so the
    # priced set unions them in (matching the on-chain redeemer's pricing scope).
    if _pool_holds_alt_supply(snapshot):
        units |= set(snapshot.market_info.alt_supply_tokens)

    prices, used_leaves = _forward_prices_and_leaves(snapshot, units)
    assert prices == captured.prices

    # The referenced leaves reproduce the captured oracle_idxs byte-exact: every priced
    # leg plus the path config's cross-check source, each mapped to its canonical
    # reference-input index (the (target, otype, idx) triples, sorted as the redeemer
    # emits them, match the captured set).
    ref_index = _captured_ref_index(fix)
    got = sorted(
        (UTxOTarget.REF, otype, ref_index[leaf.out_ref])
        for leaf, otype in used_leaves
        if leaf.out_ref is not None
    )
    assert got == sorted(captured.oracle_idxs)
