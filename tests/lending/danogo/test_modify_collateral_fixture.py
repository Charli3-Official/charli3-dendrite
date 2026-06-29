"""Offline assertions over the captured modify-collateral tx fixtures.

A modify-collateral tx adds and/or removes collateral on an existing loan WITHOUT
repaying, so the loan datum is byte-UNCHANGED in -> out (only the locked collateral
value moves) and the recorded collateral delta reflects the on-chain add/remove.
"""

import json
from pathlib import Path

import pytest

from charli3_dendrite.lending.danogo.datums import LoanDatum

FIX_DIR = Path(__file__).parent / "fixtures"
# Each fixture pairs a captured tx with the collateral change it exercises.
CASES = {
    "add": "modify_collateral_add_tx.json",
    "remove": "modify_collateral_remove_tx.json",
    "swap": "modify_collateral_swap_tx.json",
}


def _load(name: str) -> dict:
    return json.loads((FIX_DIR / name).read_text())


@pytest.fixture(params=list(CASES), ids=list(CASES))
def case(request):
    return request.param, _load(CASES[request.param])


def test_loan_datum_is_unchanged_in_to_out(case):
    """The loan datum passes through verbatim (only collateral value moves)."""
    _, fix = case
    assert fix["loan_datum_unchanged"] is True
    assert fix["loan_in_datum"] == fix["loan_out_datum"]
    # Parse both ends and confirm every datum field is identical.
    loan_in = LoanDatum.from_cbor(bytes.fromhex(fix["loan_in_datum"]))
    loan_out = LoanDatum.from_cbor(bytes.fromhex(fix["loan_out_datum"]))
    assert loan_in.to_cbor() == loan_out.to_cbor()
    assert loan_out.loan_amount == loan_in.loan_amount == fix["loan_in_amount"]
    assert loan_out.initial_interest_index == loan_in.initial_interest_index
    assert loan_out.owner_nft.to_cbor() == loan_in.owner_nft.to_cbor()
    assert loan_out.token == loan_in.token


def test_collateral_delta_matches_recorded_map(case):
    """The out-minus-in delta reconciles the captured collateral_in / collateral_out."""
    _, fix = case
    collat = fix["collateral"]
    assert collat["collateral_modified"] is True
    in_map = collat["collateral_in"]
    out_map = collat["collateral_out"]
    units = set(in_map) | set(out_map)
    recomputed = {
        u: out_map.get(u, 0) - in_map.get(u, 0)
        for u in units
        if out_map.get(u, 0) - in_map.get(u, 0) != 0
    }
    assert recomputed == collat["collateral_delta"]


def test_add_only_increases_collateral():
    fix = _load(CASES["add"])
    delta = fix["collateral"]["collateral_delta"]
    assert delta, "add fixture records a collateral change"
    assert all(v > 0 for v in delta.values())
    assert not fix["collateral"]["removed_collateral_units"]


def test_remove_only_decreases_collateral():
    fix = _load(CASES["remove"])
    delta = fix["collateral"]["collateral_delta"]
    assert delta, "remove fixture records a collateral change"
    assert all(v < 0 for v in delta.values())
    assert not fix["collateral"]["new_collateral_units"]


def test_swap_adds_a_new_type_and_removes_another():
    """The cross-type fixture introduces a genuinely new collateral unit."""
    fix = _load(CASES["swap"])
    collat = fix["collateral"]
    assert collat["new_collateral_units"], "swap introduces a new collateral type"
    assert collat["removed_collateral_units"], "swap drops a collateral type"
    delta = collat["collateral_delta"]
    # A genuine cross-type swap moves at least one unit up and one unit down.
    assert any(v > 0 for v in delta.values())
    assert any(v < 0 for v in delta.values())


def test_validity_window_within_360_slots(case):
    _, fix = case
    span = fix["invalid_hereafter"] - fix["invalid_before"]
    assert 0 < span <= 360
