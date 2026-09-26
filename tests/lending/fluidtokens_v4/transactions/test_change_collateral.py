"""Offline: forward-built V4 collateral changes reproduce the captured ones."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    build_change_collateral,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
import cbor2
from pycardano import RawPlutusData
from tests.lending.fluidtokens_v4.transactions.replay import at_minimum_ada
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_output_view
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import output_view
from tests.lending.fluidtokens_v4.transactions.replay import redeemers


@pytest.mark.parametrize(
    ("name", "loans"),
    [("change_collateral_single", 1), ("change_collateral_multi", 3)],
)
def test_change_collateral_is_byte_exact(name: str, loans: int) -> None:
    fix = fixture(name)
    built = build(
        build_change_collateral,
        ChangeCollateralSnapshot.from_capture(fix),
        slot=fix["invalid_before"],
    )
    assert redeemers(built.tx) == captured_redeemers(fix)
    # Continuing loans, then the returned bonds; the wallet change follows.
    outputs = built.tx.transaction_body.outputs
    assert [output_view(o) for o in outputs[: 2 * loans]] == [
        captured_output_view(o) for o in fix["outputs"][: 2 * loans]
    ]
    assert built.tx.transaction_body.mint is None


def test_a_loan_must_keep_some_collateral() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    snapshot.positions[0].new_collateral_amount = 0
    with pytest.raises(ValueError, match="positive collateral"):
        build(build_change_collateral, snapshot, slot=fix["invalid_before"])


def test_policy_wide_collateral_is_refused() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    datum = position.loan_datum
    none = RawPlutusData(cbor2.CBORTag(122, []))  # Option<AssetName> None
    policy_wide = replace(
        datum,
        collateral=replace(datum.collateral, maybe_asset_name=none),
    )
    position.loan = replace(position.loan, datum=policy_wide.to_cbor_hex())
    with pytest.raises(NotImplementedError, match="policy-wide"):
        build(build_change_collateral, snapshot, slot=fix["invalid_before"])


def _with_collateral(position: ChangeCollateralPosition, amount: int) -> Utxo:
    """The position's loan holding ``amount`` of its collateral."""
    unit = position.collateral_unit
    return replace(
        position.loan,
        assets=[
            (p, n, amount if p + n == unit else q) for p, n, q in position.loan.assets
        ],
    )


def _continuing(snapshot: ChangeCollateralSnapshot, fix: dict) -> TransactionOutput:
    return build(
        build_change_collateral,
        snapshot,
        slot=fix["invalid_before"],
    ).tx.transaction_body.outputs[0]


def test_a_wider_token_collateral_tops_the_ada_up_to_the_minimum() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    (held,) = [
        q for p, n, q in position.loan.assets if p + n == position.collateral_unit
    ]
    assert held < 2**32
    position.loan = at_minimum_ada(position.loan)
    position.new_collateral_amount = 2**32
    loan = _continuing(snapshot, fix)
    # A 9-byte quantity where a 5-byte one was: four more bytes at 4,310 lovelace.
    assert loan.amount.coin == min_ada(loan) == position.loan.lovelace + 4 * 4_310


def test_a_token_collateral_change_never_lowers_the_ada() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    position.new_collateral_amount = 2**32
    assert _continuing(snapshot, fix).amount.coin == position.loan.lovelace
    position.loan = at_minimum_ada(_with_collateral(position, 2**32))
    position.new_collateral_amount = 2**31
    loan = _continuing(snapshot, fix)
    assert loan.amount.coin == position.loan.lovelace > min_ada(loan)


def test_an_ada_collateral_must_cover_the_minimum_ada() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    datum = position.loan_datum
    ada = replace(
        datum.collateral,
        policy_id=b"",
        maybe_asset_name=RawPlutusData(cbor2.CBORTag(121, [b""])),  # Some("")
    )
    unit = position.collateral_unit
    position.loan = replace(
        position.loan,
        datum=replace(datum, collateral=ada).to_cbor_hex(),
        assets=[(p, n, q) for p, n, q in position.loan.assets if p + n != unit],
    )
    floor = at_minimum_ada(position.loan).lovelace
    position.new_collateral_amount = floor - 1
    with pytest.raises(ValueError, match=f"at least {floor} lovelace"):
        build(build_change_collateral, snapshot, slot=fix["invalid_before"])
    position.new_collateral_amount = floor
    assert _continuing(snapshot, fix).amount.coin == floor
