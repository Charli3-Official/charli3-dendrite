"""Modify-collateral forward oracle synthesis reproduces each captured fixture.

A modify-collateral spends only the loan (the pool is a reference input) and drives
its oracle ``Withdraw`` exactly as create-loan/repay do: the post-modification
collateral set is re-priced forward through the shared recipe / compose / structured
path machinery, and the priced source leaves become the redeemer's referenced set.
``build_modify_collateral(..., oracle_redeemer=None)`` runs that forward synthesis;
this test exercises the same `_post_modification_units` + `_forward_prices_and_leaves`
path the builder uses (see `transactions/modify_collateral.py::_modify_oracle_prep`)
fully OFFLINE -- the snapshot is reconstructed from the captured fixture JSON, with no
dbsync / Ogmios / Blockfrost -- and locks the forward result against each captured
on-chain oracle redeemer.

The decisive byte-exact proof otherwise lives only in the Ogmios e2e
(`test_modify_collateral_e2e.py`, gated on ``OGMIOS_HOST``). This guards the same
machinery offline across all three deployments / shapes:

* ``modify_collateral_add_tx`` -- structured deployment, ADA-quote market: exercises
  the new kind-0 Orcfax (Feed Status + Feed Status Pointer) and kind-1 Liqwid-market
  (market STATE + PARAM) leaf parsers, with the ``lovelace`` intermediate taken as the
  MIN across resolving deviation paths.
* ``modify_collateral_remove_tx`` -- packed/legacy deployment, cross-quote market:
  exercises the mined USDM recipe + ``compose_cross_quote`` intermediate and its
  deviation cross-check source leaf.
* ``modify_collateral_swap_tx`` -- structured deployment, multi-path: exercises the
  structured multi-path averaging (primary path declared, alternative paths referenced
  as deviation cross-checks).

What is asserted per fixture: the forward ``prices`` map equals the captured
redeemer's prices map EXACTLY (same quote, units, exact rationals), and the referenced
source-leaf SET equals the captured redeemer's leaf set EXACTLY (each priced leaf
mapped to its canonical reference-input index).

What is INTENTIONALLY NOT pinned: the ``oracle_idxs`` LIST ORDERING. The off-chain
builder picks that order arbitrarily (it is validity-irrelevant -- the on-chain
validator reads the leaves by index, not by position), so the leaves are compared as a
SET (sorted), NOT as an ordered list, and the full redeemer CBOR is NOT asserted equal.
"""

import json
from pathlib import Path
from typing import Callable

import pytest

from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.transactions._common import (
    _forward_prices_and_leaves,
)
from charli3_dendrite.lending.danogo.transactions.context import (
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.danogo.transactions.modify_collateral import (
    _post_modification_units,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Each modify fixture forward-synthesizes its oracle Withdraw from live source leaves
# (the builder takes ``oracle_redeemer=None``); the captured on-chain redeemer is the
# ground truth the forward result must reproduce. The three span both deployments and
# every multi-leaf source kind the shared pricer handles.
FIXTURES = [
    "modify_collateral_add_tx.json",
    "modify_collateral_remove_tx.json",
    "modify_collateral_swap_tx.json",
]


def _captured_ref_index(fix: dict) -> dict[tuple[str, int], int]:
    """Map each reference input out-ref to its canonical Plutus script-context index."""
    refs = [(u["out_ref"][0], int(u["out_ref"][1])) for u in fix["ref_inputs"]]
    refs.sort(key=lambda r: (bytes.fromhex(r[0]), r[1]))
    return {r: pos for pos, r in enumerate(refs)}


def _target_collateral(fix: dict) -> dict[str, int]:
    """The loan output's TARGET collateral (the set the oracle Withdraw prices)."""
    return {unit: int(qty) for unit, qty in fix["collateral"]["collateral_out"].items()}


@pytest.mark.parametrize("fixture", FIXTURES)
def test_modify_forward_oracle_reproduces_captured_redeemer(
    modify_snap: Callable[[str], tuple[dict, ModifyCollateralSnapshot]],
    fixture: str,
) -> None:
    fix, snapshot = modify_snap(fixture)
    captured = OraclePriceCalcRdmr.from_cbor(fix["oracle_redeemer"])

    # Forward-synthesize the oracle exactly as the builder does with
    # ``oracle_redeemer=None``: price the loan OUTPUT's (target) collateral through the
    # shared forward machinery (`_modify_oracle_prep` wraps this same call).
    collateral_units = _post_modification_units(snapshot, _target_collateral(fix))
    prices, used_leaves = _forward_prices_and_leaves(snapshot, set(collateral_units))

    # The prices map reproduces the captured redeemer EXACTLY: same quote, same units,
    # same exact rationals (including the cross-quote ADA intermediate where present).
    assert prices == captured.prices

    # The referenced source-leaf SET reproduces the captured redeemer's leaf set
    # EXACTLY -- each priced leaf mapped to its canonical reference-input index. The
    # ``oracle_idxs`` LIST ORDER is the off-chain builder's arbitrary (validity-
    # irrelevant) choice and is deliberately NOT pinned, so leaves are compared as a
    # SET (sorted), not as an ordered list, and the full redeemer CBOR is not asserted.
    ref_index = _captured_ref_index(fix)
    got = sorted(
        (UTxOTarget.REF, otype, ref_index[leaf.out_ref])
        for leaf, otype in used_leaves
        if leaf.out_ref is not None
    )
    assert got == sorted(captured.oracle_idxs)
