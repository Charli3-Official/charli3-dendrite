"""IncreaseLoanSnapshot resolves the live building blocks an increase-loan needs.

Three layers:

- Offline (always runs): from the captured increase-loan fixture, parse the loan
  input's `LoanDatum`, confirm the owner-NFT unit derives correctly (it must match the
  borrower input's owner NFT), and confirm the loan-token / collateral split the
  snapshot keys on.
- Health-factor preflight (offline, from the reconstructed snapshot): `safe_increase_amount`
  accepts the captured borrow (the post-increase collateral value exceeds the new loan
  amount and utilization stays within cap) and rejects an over-large borrow.
- Live (gated on a reachable db-sync): resolve an `IncreaseLoanSnapshot` for a
  currently-open loan and assert the loan UTxO, owner-NFT unit, pool, market, script
  references, and oracle reference UTxOs all resolve.
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
    safe_increase_amount,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    IncreaseLoanSnapshot,
    _loan_collateral_units,
    _parse_out_ref,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    Utxo as CtxUtxo,
)

FIXTURE = "increase_loan_tx.json"
FIX = json.loads((Path(__file__).parent / "fixtures" / FIXTURE).read_text())

INCREASE_LOAN_UTXO = os.environ.get("DANOGO_TEST_INCREASE_LOAN_UTXO", "")
INCREASE_MARKET = os.environ.get("DANOGO_TEST_INCREASE_MARKET", "")


def _fixture_loan_utxo() -> CtxUtxo:
    """The fixture's loan input as a context `Utxo` (the UTxO the snapshot resolves)."""
    loan_skh = FIX["loan_skh"]
    market_name = FIX["market_name"]
    for u in FIX["inputs"]:
        assets = [(p, n, int(q)) for p, n, q in u["assets"]]
        if any(p == loan_skh and n == market_name and q == 1 for p, n, q in assets):
            return CtxUtxo(
                address=u["address"],
                lovelace=int(u["lovelace"]),
                assets=assets,
                datum=u["datum"],
                out_ref=tuple(u["out_ref"]),
            )
    raise AssertionError("fixture has no loan input carrying the market loan token")


def test_owner_nft_and_collateral_derive_from_fixture_loan():
    """The owner-NFT unit + collateral split derive correctly from a real loan datum."""
    loan_skh = FIX["loan_skh"]
    market_name = FIX["market_name"]
    loan = _fixture_loan_utxo()

    loan_datum = LoanDatum.from_cbor(bytes.fromhex(loan.datum))
    owner_nft = loan_datum.owner_nft.unit()

    # No mint on an increase, so the owner NFT is identified by the borrower input that
    # holds it (qty 1), not a burn entry. It is a loan-script-minted token.
    assert owner_nft.startswith(loan_skh)
    borrower_holds_owner = any(
        any(p + n == owner_nft and int(q) == 1 for p, n, q in u["assets"])
        for u in FIX["inputs"]
    )
    assert borrower_holds_owner
    assert not FIX["mints"]

    # The borrowed token recorded in the datum is the market's supply token.
    assert loan_datum.token_unit() == FIX["supply_token"]

    # The collateral split keeps everything but the loan token; the locked collateral
    # (here a pool dToken) is carried through for the oracle to re-price.
    collateral = _loan_collateral_units(
        loan, loan_skh=loan_skh, market_name=market_name
    )
    assert loan_skh + market_name not in collateral
    assert collateral  # the fixture loan locks at least one collateral asset
    assert all(unit != "lovelace" for unit in collateral)


def test_safe_increase_amount_accepts_captured_borrow(
    increase_snap: Callable[[str], tuple[dict, IncreaseLoanSnapshot]],
):
    """The captured (valid) borrow clears the post-increase HF + utilization preflight."""
    fix, snap = increase_snap(FIXTURE)
    realized = fix["realized"]
    borrow = -realized["pool_changed_amount"]
    new_loan_amount = realized["current_loan_amount"] + borrow

    headroom = safe_increase_amount(
        snap,
        borrow_amount=borrow,
        txn_time=realized["txn_time"],
    )
    # The threshold-weighted collateral value exceeds the new (larger) loan amount.
    assert headroom > new_loan_amount


def test_safe_increase_amount_rejects_excessive_borrow(
    increase_snap: Callable[[str], tuple[dict, IncreaseLoanSnapshot]],
):
    """An over-large borrow fails the preflight loud (the loan would be unhealthy)."""
    fix, snap = increase_snap(FIXTURE)
    realized = fix["realized"]
    # Borrow far beyond what the collateral can back.
    with pytest.raises(ValueError, match="under-collateralized|utilization"):
        safe_increase_amount(
            snap,
            borrow_amount=realized["current_loan_amount"] * 1000,
            txn_time=realized["txn_time"],
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
def test_increase_loan_snapshot_resolves_a_live_open_loan():
    import psycopg

    from charli3_dendrite.backend.dbsync import DbsyncBackend

    backend = DbsyncBackend()
    try:
        if INCREASE_LOAN_UTXO and INCREASE_MARKET:
            market_name, loan_utxo = INCREASE_MARKET, INCREASE_LOAN_UTXO
        else:
            market_name, loan_utxo = _discover_open_loan(backend)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"db-sync unreachable: {exc}")
    except LookupError as exc:
        pytest.skip(str(exc))

    try:
        snap = IncreaseLoanSnapshot.from_backend(
            backend,
            market_name=market_name,
            loan_utxo=loan_utxo,
        )
    except ValueError as exc:
        # A discovered loan whose collateral has a recipe but an unresolvable leaf is a
        # data-availability gap, not a resolver bug; skip rather than fail the suite.
        pytest.skip(f"live loan {loan_utxo} not fully resolvable: {exc}")

    # The loan UTxO resolves to the requested out-ref, sits at the loan script address,
    # carries the market loan token, and its datum parsed.
    assert snap.loan.out_ref == _parse_out_ref(loan_utxo)
    assert snap.loan.holds(snap.loan_skh, market_name, 1)
    assert isinstance(snap.loan_datum, LoanDatum)

    # The owner-NFT unit derives from the loan datum and is a loan-script-minted token.
    assert snap.owner_nft == snap.loan_datum.owner_nft.unit()
    assert snap.owner_nft.startswith(snap.loan_skh)

    # The shared pool-action prefix resolved: pool + market paired by pool-NFT name.
    assert snap.market.out_ref is not None
    assert snap.pool.out_ref is not None
    assert snap.market.holds(snap.config_pool_skh, market_name)
    assert snap.pool.holds(snap.config_pool_skh, market_name)

    # The three reference scripts the forward build attaches (the pool script ref also
    # carries the Withdraw(pool_skh) hub script).
    for ref in (
        snap.pool_script_ref,
        snap.loan_mint_script_ref,
        snap.oracle_script_ref,
    ):
        assert ref.ref_script is not None
        assert ref.out_ref is not None

    # The Danogo-owned oracle reference UTxOs (config / path) resolved.
    assert snap.oracle_data_refs
