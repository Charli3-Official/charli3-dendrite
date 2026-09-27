"""Gated: V4 repayment claims evaluate on Ogmios against mainnet (OGMIOS_HOST).

No mainnet claim collects several repayments or several bonds at once, so those
claims spend copies of the captured bond and repayment at other out-refs: Ogmios
reads them from ``additionalUtxo``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import (
    MAX_REPAYMENTS_PER_CLAIM,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import ClaimPosition
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import ClaimSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import build_claim
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios

pytestmark = needs_ogmios

_WITHDRAWS = ["withdraw"] * 3


def _claim(spends: int) -> list[str]:
    """The evaluated purposes of a claim spending ``spends`` UTxOs."""
    return sorted(["spend"] * spends + _WITHDRAWS)


def _capture(name: str) -> tuple[dict, ClaimSnapshot, ClaimPosition]:
    fix = fixture(name)
    snapshot = ClaimSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    return fix, snapshot, position


def _repayment_copy(repayment: Utxo, out_ref: tuple[str, int], owner: bytes) -> Utxo:
    """``repayment`` at ``out_ref``, owned by the lender bond ``owner``."""
    datum = AssetManagerDatumWithToken.from_cbor(repayment.datum or "")
    datum.owner_asset.asset_name = owner
    return replace(repayment, out_ref=out_ref, datum=datum.to_cbor_hex())


@pytest.mark.parametrize("name", ["claim", "claim_bond_first"])
def test_captured_claim_evaluates(name: str) -> None:
    fix, snapshot, _ = _capture(name)
    built = build(build_claim, snapshot, slot=fix["invalid_before"])
    assert evaluate(built, fixture_utxos(fix)) == _claim(2)


def test_one_bond_claims_two_repayments() -> None:
    fix, snapshot, position = _capture("claim")
    (repayment,) = position.repayments
    (name,) = position.bond_names
    second = _repayment_copy(repayment, ("dd" * 32, 0), name)
    built = build(
        build_claim,
        replace(
            snapshot,
            positions=[replace(position, repayments=[repayment, second])],
        ),
        slot=fix["invalid_before"],
    )
    assert evaluate(built, [*fixture_utxos(fix), second]) == _claim(3)


def test_two_bonds_claim_together() -> None:
    fix, snapshot, position = _capture("claim")
    (repayment,) = position.repayments
    (name,) = position.bond_names
    other_name = b"\x01" + name[1:]
    bond = replace(
        position.bond,
        out_ref=("dc" * 32, 0),
        assets=[
            (p, other_name.hex() if p == c.LENDER_BOND_POLICY else n, q)
            for p, n, q in position.bond.assets
        ],
    )
    other = ClaimPosition(
        bond=bond,
        repayments=[_repayment_copy(repayment, ("dd" * 32, 0), other_name)],
    )
    built = build(
        build_claim,
        replace(snapshot, positions=[position, other]),
        slot=fix["invalid_before"],
    )
    utxos = [*fixture_utxos(fix), other.bond, *other.repayments]
    assert evaluate(built, utxos) == _claim(4)


def test_a_claim_at_its_cap_evaluates_among_many_inputs() -> None:
    # The asset manager checks each repayment against every input; the cap must
    # leave room for a wallet's funding inputs.
    fix, snapshot, position = _capture("claim")
    (repayment,) = position.repayments
    (name,) = position.bond_names
    copies = [
        _repayment_copy(repayment, (f"{index + 1:064x}", 0), name)
        for index in range(MAX_REPAYMENTS_PER_CLAIM - 1)
    ]
    wallet = fix["outputs"][1]["address"]
    funding = [
        Utxo(
            address=wallet,
            lovelace=5_000_000,
            assets=[],
            datum=None,
            out_ref=(f"f{index:063x}", 0),
        )
        for index in range(20)
    ]
    built = build(
        build_claim,
        replace(
            snapshot,
            positions=[replace(position, repayments=[repayment, *copies])],
            funding=funding,
        ),
        slot=fix["invalid_before"],
    )
    utxos = [*fixture_utxos(fix), *copies, *funding]
    assert evaluate(built, utxos) == _claim(1 + MAX_REPAYMENTS_PER_CLAIM)
