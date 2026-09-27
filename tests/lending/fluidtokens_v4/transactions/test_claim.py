"""Offline: forward-built V4 repayment claims reproduce the captured mainnet claims."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSpendScript
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import (
    MAX_REPAYMENTS_PER_CLAIM,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import ClaimSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import build_claim
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.transactions.infra import EvalContext
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_output_view
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import claim_fixture_backend
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import output_view
from tests.lending.fluidtokens_v4.transactions.replay import redeemers

CAPTURES = ["claim", "claim_bond_first"]
# The lenders who signed the captured claims.
_LENDER = {
    "claim": "1c471b31",
    "claim_bond_first": "6fae7995",
}


def _copies(repayment: Utxo, amounts: list[int]) -> list[Utxo]:
    """``repayment`` at other out-refs, holding each of ``amounts`` lovelace."""
    return [
        replace(repayment, out_ref=(f"{index + 1:064x}", 0), lovelace=amount)
        for index, amount in enumerate(amounts)
    ]


def _capture(name: str) -> tuple[dict, ClaimSnapshot]:
    fix = fixture(name)
    return fix, ClaimSnapshot.from_capture(fix)


@pytest.mark.parametrize("name", CAPTURES)
def test_claim_redeemers_are_byte_exact(name: str) -> None:
    fix, snapshot = _capture(name)
    built = build(build_claim, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)


@pytest.mark.parametrize("name", CAPTURES)
def test_claim_returns_the_bond_and_needs_the_lender(name: str) -> None:
    fix, snapshot = _capture(name)
    body = build(build_claim, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    # The bond goes back to the lender manager unchanged; the repayment is change.
    assert [output_view(o) for o in body.outputs] == [
        captured_output_view(fix["outputs"][0]),
    ]
    assert [s.payload.hex()[:8] for s in body.required_signers] == [_LENDER[name]]
    lender_manager = c.LENDER_MANAGER_WITHDRAW_SKH
    assert set(body.withdraws) == {
        reward_address(h)
        for h in (
            lender_manager,
            snapshot.withdraw_bonds_script_hash,
            c.REPAYMENT_POLICY,
        )
    }
    assert body.mint is None


def test_claim_records_the_funding_for_ogmios() -> None:
    fix, snapshot = _capture("claim")
    wallet = Utxo(
        address=fix["outputs"][1]["address"],
        lovelace=5_000_000,
        assets=[],
        datum=None,
        out_ref=("ee" * 32, 0),
    )
    funded = replace(snapshot, funding=[wallet])
    build(build_claim, funded, slot=fix["invalid_before"])
    assert len(funded.additional_utxo()) == 1


def test_a_repayment_the_bond_does_not_own_is_refused() -> None:
    fix, snapshot = _capture("claim")
    (position,) = snapshot.positions
    (repayment,) = position.repayments
    datum = AssetManagerDatumWithToken.from_cbor(repayment.datum)
    datum.owner_asset.asset_name = b"\x01" * 28
    stranger = replace(repayment, datum=datum.to_cbor_hex())
    with pytest.raises(ValueError, match="does not own"):
        build(
            build_claim,
            replace(snapshot, positions=[replace(position, repayments=[stranger])]),
            slot=fix["invalid_before"],
        )


def test_a_bond_without_repayments_is_refused() -> None:
    fix, snapshot = _capture("claim")
    (position,) = snapshot.positions
    with pytest.raises(ValueError, match="no repayment"):
        build(
            build_claim,
            replace(snapshot, positions=[replace(position, repayments=[])]),
            slot=fix["invalid_before"],
        )
    with pytest.raises(ValueError, match="at least one lender bond"):
        build(build_claim, replace(snapshot, positions=[]), slot=0)


def test_a_bond_not_owned_by_a_key_is_refused() -> None:
    fix, snapshot = _capture("claim")
    (position,) = snapshot.positions
    datum = LenderManagerDatum.from_cbor(position.bond.datum)
    datum.lender_auth = AuthCardanoSpendScript(script_hash=b"\x01" * 28)
    bond = replace(position.bond, datum=datum.to_cbor_hex())
    with pytest.raises(NotImplementedError, match="owned by a key"):
        build(
            build_claim,
            replace(snapshot, positions=[replace(position, bond=bond)]),
            slot=fix["invalid_before"],
        )


def test_a_claim_is_the_only_lender_manager_action() -> None:
    fix, snapshot = _capture("claim")
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    tx_builder.withdrawals = {  # type: ignore[assignment]
        reward_address(c.LENDER_MANAGER_WITHDRAW_SKH): 0,
    }
    with pytest.raises(ValueError, match="only lender-manager action"):
        build_claim(tx_builder, snapshot=snapshot)
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    tx_builder.add_input(to_pycardano_utxo(snapshot.positions[0].bond))
    with pytest.raises(ValueError, match="only lender-manager action"):
        build_claim(tx_builder, snapshot=snapshot)


def test_a_claim_spends_at_most_its_cap_of_repayments() -> None:
    fix, snapshot = _capture("claim")
    (position,) = snapshot.positions
    (repayment,) = position.repayments
    extra = _copies(repayment, [100_000_000] * MAX_REPAYMENTS_PER_CLAIM)
    with pytest.raises(ValueError, match=f"at most {MAX_REPAYMENTS_PER_CLAIM}"):
        build(
            build_claim,
            replace(
                snapshot,
                positions=[replace(position, repayments=[repayment, *extra])],
            ),
            slot=fix["invalid_before"],
        )


def test_a_bond_below_its_minimum_ada_is_topped_up() -> None:
    fix, snapshot = _capture("claim")
    (position,) = snapshot.positions
    short = replace(position, bond=replace(position.bond, lovelace=1_000_000))
    body = build(
        build_claim,
        replace(snapshot, positions=[short]),
        slot=fix["invalid_before"],
    ).tx.transaction_body
    (bond,) = body.outputs
    assert bond.amount.coin == min_ada(bond) > 1_000_000  # noqa: PLR2004
    assert bond.datum.to_cbor().hex() == position.bond.datum


@pytest.mark.parametrize("name", CAPTURES)
def test_from_backend_finds_the_lenders_repayments(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    fix, capture = _capture(name)
    claim_fixture_backend(monkeypatch, fix)
    snapshot = ClaimSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        lender_address=fix["outputs"][1]["address"],
        funding=[],
    )
    assert [p.bond.out_ref for p in snapshot.positions] == [
        p.bond.out_ref for p in capture.positions
    ]
    assert [[r.out_ref for r in p.repayments] for p in snapshot.positions] == [
        [r.out_ref for r in p.repayments] for p in capture.positions
    ]
    built = build(build_claim, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)


def test_from_backend_claims_only_the_named_bonds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture("claim")
    claim_fixture_backend(monkeypatch, fix)
    lender = fix["outputs"][1]["address"]
    (bond_name,) = capture.positions[0].bond_names
    snapshot = ClaimSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        lender_address=lender,
        bonds=[bond_name],
        funding=[],
    )
    assert len(snapshot.positions) == 1
    with pytest.raises(ValueError, match="not at the lender manager"):
        ClaimSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            lender_address=lender,
            bonds=[b"\x01" * 28],
            funding=[],
        )


def test_from_backend_refuses_a_lender_with_nothing_to_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, _ = _capture("claim")
    claim_fixture_backend(monkeypatch, fix)
    other = fixture("claim_bond_first")["outputs"][1]["address"]
    with pytest.raises(ValueError, match="no repayments to claim"):
        ClaimSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            lender_address=other,
            funding=[],
        )


def test_from_backend_refuses_a_wallet_with_no_funding_utxos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve

    fix, _ = _capture("claim")
    claim_fixture_backend(monkeypatch, fix)
    monkeypatch.setattr(resolve, "resolve_wallet_funding", lambda *_, **__: [])
    lender = fix["outputs"][1]["address"]
    with pytest.raises(ValueError, match="fund the claim"):
        ClaimSnapshot.from_backend(
            object(),  # type: ignore[arg-type]
            lender_address=lender,
        )


def test_from_backend_keeps_the_largest_repayments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, capture = _capture("claim")
    (repayment,) = capture.positions[0].repayments
    extra = _copies(repayment, [(index + 1) * 1_000_000 for index in range(11)])
    claim_fixture_backend(monkeypatch, fix, extra=extra)
    snapshot = ClaimSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        lender_address=fix["outputs"][1]["address"],
        funding=[],
    )
    (position,) = snapshot.positions
    kept = sorted(r.lovelace for r in position.repayments)
    # The captured 100 ADA repayment and the nine largest copies; 1 and 2 ADA wait.
    assert len(kept) == MAX_REPAYMENTS_PER_CLAIM
    assert kept == [amount * 1_000_000 for amount in range(3, 12)] + [100_000_000]


def test_from_backend_skips_a_repayment_without_an_inline_datum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve

    fix, capture = _capture("claim")
    (repayment,) = capture.positions[0].repayments
    (decoy,) = _copies(repayment, [2_000_000])
    claim_fixture_backend(monkeypatch, fix, extra=[decoy])
    resolved = resolve.resolve_utxo
    # Indexed through its datum hash, the decoy resolves without an inline datum.
    monkeypatch.setattr(
        resolve,
        "resolve_utxo",
        lambda backend, out_ref, **kw: (
            replace(decoy, datum=None)
            if out_ref == decoy.out_ref
            else resolved(backend, out_ref, **kw)
        ),
    )
    snapshot = ClaimSnapshot.from_backend(
        object(),  # type: ignore[arg-type]
        lender_address=fix["outputs"][1]["address"],
        funding=[],
    )
    (position,) = snapshot.positions
    assert [r.out_ref for r in position.repayments] == [repayment.out_ref]
