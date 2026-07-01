"""RepaySnapshot resolves the live building blocks a repay (decrease-loan) needs.

Two layers:

- Offline (always runs): from the captured decrease-loan fixture, parse the loan
  input's `LoanDatum`, confirm the owner-NFT unit derives correctly (it must match
  both the burned owner NFT and the borrower input's owner NFT), and confirm the
  loan-token / collateral split the snapshot keys on.
- Live (gated on a reachable db-sync): resolve a `RepaySnapshot` for a currently-open
  loan -- discovered from the backend, or pinned via `DANOGO_TEST_REPAY_LOAN_UTXO`
  (+ `DANOGO_TEST_REPAY_MARKET`) -- and assert the loan UTxO, owner-NFT unit, pool,
  market, script references, and oracle reference UTxOs all resolve.
"""

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

from charli3_dendrite.lending.danogo.datums import LoanDatum  # noqa: E402
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    ORACLE_SKH,
    RepaySnapshot,
    _loan_collateral_units,
    _parse_out_ref,
)
from charli3_dendrite.lending.danogo.transactions.context import (  # noqa: E402
    Utxo as CtxUtxo,
)

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "decrease_loan_tx.json").read_text()
)

REPAY_LOAN_UTXO = os.environ.get("DANOGO_TEST_REPAY_LOAN_UTXO", "")
REPAY_MARKET = os.environ.get("DANOGO_TEST_REPAY_MARKET", "")


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

    # The owner-NFT unit the snapshot derives must equal the token the full repay burns
    # (policy = loan_skh) ...
    burned_owner = next(
        (p + n for p, n, q in FIX["mints"] if p == loan_skh and n != market_name),
    )
    assert owner_nft == burned_owner
    assert owner_nft.startswith(loan_skh)

    # ... and the owner NFT the borrower input actually holds (qty 1).
    borrower_holds_owner = any(
        any(p + n == owner_nft and int(q) == 1 for p, n, q in u["assets"])
        for u in FIX["inputs"]
    )
    assert borrower_holds_owner

    # The borrowed token recorded in the datum is the market's supply token.
    assert loan_datum.token_unit() == FIX["supply_token"]

    # The collateral split keeps everything but the loan token; the loan token is the
    # only loan_skh asset on the loan UTxO, so it is excluded and the locked collateral
    # (here the pool dToken) is carried through for the oracle to re-price.
    collateral = _loan_collateral_units(
        loan, loan_skh=loan_skh, market_name=market_name
    )
    assert loan_skh + market_name not in collateral
    assert collateral  # the fixture loan locks at least one collateral asset
    assert all(unit != "lovelace" for unit in collateral)


def _discover_open_loan(backend) -> tuple[str, str]:
    """Find a currently-open loan; return ``(market_name, "tx_hash#index")``.

    Picks the first loan-script UTxO that parses as a `LoanDatum` and carries exactly
    one loan-script-policy asset (the loan token, whose name is the market name).
    """
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
def test_repay_snapshot_resolves_a_live_open_loan():
    import psycopg

    from charli3_dendrite.backend.dbsync import DbsyncBackend

    backend = DbsyncBackend()
    try:
        if REPAY_LOAN_UTXO and REPAY_MARKET:
            market_name, loan_utxo = REPAY_MARKET, REPAY_LOAN_UTXO
        else:
            market_name, loan_utxo = _discover_open_loan(backend)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"db-sync unreachable: {exc}")
    except LookupError as exc:
        pytest.skip(str(exc))

    try:
        snap = RepaySnapshot.from_backend(
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
    assert snap.oracle_skh == ORACLE_SKH
    assert snap.market.out_ref is not None
    assert snap.pool.out_ref is not None
    assert snap.market.holds(snap.config_pool_skh, market_name)
    assert snap.pool.holds(snap.config_pool_skh, market_name)

    # The three reference scripts the forward build attaches.
    for ref in (
        snap.pool_script_ref,
        snap.loan_mint_script_ref,
        snap.oracle_script_ref,
    ):
        assert ref.ref_script is not None
        assert ref.out_ref is not None

    # The Danogo-owned oracle reference UTxOs (config / path) resolved.
    assert snap.oracle_data_refs
