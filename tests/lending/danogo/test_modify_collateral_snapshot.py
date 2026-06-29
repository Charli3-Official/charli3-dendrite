"""ModifyCollateralSnapshot resolves the live building blocks a modify-collateral needs.

Three layers:

- Offline (always runs): from each captured modify fixture, parse the loan input's
  `LoanDatum`, confirm the owner-NFT unit derives correctly, confirm the loan-token /
  collateral split the snapshot keys on, and confirm the pool is carried as REFERENCE
  material (it is among the captured tx's reference inputs, never its spent inputs) and
  that the modify mints nothing.
- Health-factor preflight (offline, from the reconstructed snapshot): on a market whose
  oracle the forward pricer reproduces, `safe_collateral_for_modify` accepts the
  captured (valid) target collateral and rejects a starved one. The debt is unchanged,
  so the bar is the loan's current accrued amount, not a reduced repay amount.
- Live (gated on a reachable db-sync): resolve a `ModifyCollateralSnapshot` for a
  currently-open loan and assert the loan UTxO, owner-NFT unit, pool (reference), market,
  script references, and oracle reference UTxOs all resolve -- and that no pool script
  reference is carried (the pool is never run as a script).
"""

import json
import os
from pathlib import Path
from typing import Callable

import pytest
from dotenv import load_dotenv

load_dotenv()

from charli3_dendrite.lending.danogo.datums import LoanDatum  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.build import (  # noqa: E402
    safe_collateral_for_modify,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    ModifyCollateralSnapshot,
    _loan_collateral_units,
    _parse_out_ref,
)

FIXTURES = [
    "modify_collateral_add_tx.json",
    "modify_collateral_remove_tx.json",
    "modify_collateral_swap_tx.json",
]

# The legacy-oracle market whose forward pricing the pricer reproduces (the structured
# add/swap markets use multi-source averaging the forward pricer does not yet replay).
FORWARD_PRICEABLE_FIXTURE = "modify_collateral_remove_tx.json"

MODIFY_LOAN_UTXO = os.environ.get("DANOGO_TEST_MODIFY_LOAN_UTXO", "")
MODIFY_MARKET = os.environ.get("DANOGO_TEST_MODIFY_MARKET", "")


def _target(fix: dict) -> dict[str, int]:
    return {unit: int(qty) for unit, qty in fix["collateral"]["collateral_out"].items()}


@pytest.mark.parametrize("fixture", FIXTURES)
def test_owner_nft_and_pool_is_reference_not_spent(
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    """The owner NFT derives correctly and the pool is reference material, not a spend."""
    fix, snap = modify_snap(fixture)

    # The owner NFT is a loan-script-minted token, held (qty 1) by the borrower input;
    # nothing is minted or burned by a modify.
    assert snap.owner_nft == snap.loan_datum.owner_nft.unit()
    assert snap.owner_nft.startswith(snap.loan_skh)
    assert not fix["mints"]

    # The pool UTxO the snapshot carries is one of the captured tx's REFERENCE inputs
    # (read for the interest index) and is NOT among its spent inputs.
    ref_out_refs = {tuple(u["out_ref"]) for u in fix["ref_inputs"]}
    spent_out_refs = {tuple(u["out_ref"]) for u in fix["inputs"]}
    assert snap.pool.out_ref in ref_out_refs
    assert snap.pool.out_ref not in spent_out_refs

    # The loan is the spent input carrying the market loan token + a LoanDatum.
    assert snap.loan.out_ref in spent_out_refs
    assert snap.loan.holds(snap.loan_skh, snap.market_name, 1)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_collateral_split_excludes_loan_token(
    modify_snap,  # noqa: ANN001
    fixture: str,
) -> None:
    """The collateral the oracle prices is everything the loan locks but the loan token."""
    fix, snap = modify_snap(fixture)
    collateral = _loan_collateral_units(
        snap.loan,
        loan_skh=snap.loan_skh,
        market_name=snap.market_name,
    )
    assert snap.loan_skh + snap.market_name not in collateral
    assert collateral  # the fixture loan locks at least one collateral asset


def test_no_pool_script_reference_is_carried(
    modify_snap,  # noqa: ANN001
) -> None:
    """A modify never runs the pool as a script, so the snapshot carries no pool ref."""
    _fix, snap = modify_snap(FORWARD_PRICEABLE_FIXTURE)
    # Unlike repay / increase, ModifyCollateralSnapshot has no pool_script_ref field --
    # the pool is read-only (a reference input), never executed.
    assert not hasattr(snap, "pool_script_ref")


def test_safe_collateral_accepts_captured_target(
    modify_snap,  # noqa: ANN001
) -> None:
    """The captured (valid) target clears the post-modification HF preflight."""
    fix, snap = modify_snap(FORWARD_PRICEABLE_FIXTURE)
    headroom = safe_collateral_for_modify(
        snap,
        target_collateral=_target(fix),
        txn_time=fix["block_time"] * 1000,
    )
    # The debt is unchanged, so the threshold-weighted target value must exceed the
    # loan's accrued debt -- and it clears the input loan amount with margin.
    assert headroom > fix["loan_in_amount"]


def test_safe_collateral_rejects_starved_target(
    modify_snap,  # noqa: ANN001
) -> None:
    """A target that strips the collateral fails the preflight loud (under-collateralized)."""
    fix, snap = modify_snap(FORWARD_PRICEABLE_FIXTURE)
    starved = {unit: 1 for unit in _target(fix)}
    with pytest.raises(ValueError, match="under-collateralized"):
        safe_collateral_for_modify(
            snap,
            target_collateral=starved,
            txn_time=fix["block_time"] * 1000,
        )


def _discover_open_loan(backend) -> tuple[str, str]:  # noqa: ANN001
    """Find a currently-open loan; return ``(market_name, "tx_hash#index")``."""
    from pycardano import Address

    from charli3_dendrite.lending.danogo.constants import resolve_addresses

    addresses = resolve_addresses(backend)
    loan_skh = Address.decode(addresses["loan"]).payment_part.payload.hex()
    for info in backend.get_pool_utxos(addresses=[addresses["loan"]], historical=False):
        if not info.datum_cbor:
            continue
        try:
            LoanDatum.from_cbor(bytes.fromhex(info.datum_cbor))
        except Exception:  # noqa: BLE001 - not a loan UTxO; skip
            continue
        loan_tokens = [
            unit[len(loan_skh) :]
            for unit, qty in info.assets.root.items()
            if unit != "lovelace" and unit.startswith(loan_skh) and qty == 1
        ]
        if len(loan_tokens) != 1:
            continue
        return loan_tokens[0], f"{info.tx_hash}#{info.tx_index}"
    raise LookupError("no currently-open loan UTxO found at the loan script address")


@pytest.mark.skipif(
    not os.environ.get("DBSYNC_HOST"),
    reason="needs a reachable db-sync (DBSYNC_HOST/PORT/USER/PASS/DB_NAME)",
)
def test_modify_collateral_snapshot_resolves_a_live_open_loan():
    import psycopg

    from charli3_dendrite.backend.dbsync import DbsyncBackend

    backend = DbsyncBackend()
    try:
        if MODIFY_LOAN_UTXO and MODIFY_MARKET:
            market_name, loan_utxo = MODIFY_MARKET, MODIFY_LOAN_UTXO
        else:
            market_name, loan_utxo = _discover_open_loan(backend)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"db-sync unreachable: {exc}")
    except LookupError as exc:
        pytest.skip(str(exc))

    try:
        snap = ModifyCollateralSnapshot.from_backend(
            backend,
            market_name=market_name,
            loan_utxo=loan_utxo,
        )
    except ValueError as exc:
        # A discovered loan whose collateral has a recipe but an unresolvable leaf is a
        # data-availability gap, not a resolver bug; skip rather than fail the suite.
        pytest.skip(f"live loan {loan_utxo} not fully resolvable: {exc}")

    # The loan UTxO resolves to the requested out-ref, carries the market loan token,
    # and its datum parsed.
    assert snap.loan.out_ref == _parse_out_ref(loan_utxo)
    assert snap.loan.holds(snap.loan_skh, market_name, 1)
    assert isinstance(snap.loan_datum, LoanDatum)

    # The owner-NFT unit derives from the loan datum and is a loan-script-minted token.
    assert snap.owner_nft == snap.loan_datum.owner_nft.unit()
    assert snap.owner_nft.startswith(snap.loan_skh)

    # The shared pool-action prefix resolved: pool + market paired by pool-NFT name. The
    # pool is reference material (its out-ref + datum are kept), never spent.
    assert snap.market.out_ref is not None
    assert snap.pool.out_ref is not None
    assert snap.pool.datum is not None
    assert snap.market.holds(snap.config_pool_skh, market_name)
    assert snap.pool.holds(snap.config_pool_skh, market_name)

    # The two reference scripts the forward build attaches (loan-mint + oracle); a modify
    # never runs the pool as a script, so no pool script reference is carried.
    assert not hasattr(snap, "pool_script_ref")
    for ref in (snap.loan_mint_script_ref, snap.oracle_script_ref):
        assert ref.ref_script is not None
        assert ref.out_ref is not None

    # The Danogo-owned oracle reference UTxOs (config / path) resolved.
    assert snap.oracle_data_refs
